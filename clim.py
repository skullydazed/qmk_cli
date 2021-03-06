# coding=utf-8
"""py-clim - The CLI Context Manager

CLIM is an opinionated framework for writing CLI apps. It optimizes for the
most common unix tool pattern- small tools that are run from the command
line but generally do not feature any user interaction while they run.

Using CLIM will give your script all of these features with little or no
work on your part:

* CLI Argument Parsing, with or without subcommands
* Performance improvement from putting your code inside a function
  <https://stackoverflow.com/questions/11241523/why-does-python-code-run-faster-in-a-function>
* Config file support, with config options overridden by command line flags
* Logging to stderr and/or a file
* Thread safety! (Note: This needs more eyes looking at it.)
"""
from __future__ import division, print_function, unicode_literals
import argparse
import logging
import os.path
import re
import sys
from decimal import Decimal
from tempfile import NamedTemporaryFile
from time import sleep

try:
    from ConfigParser import RawConfigParser
except ImportError:
    from configparser import RawConfigParser

try:
    import thread
    import threading
except ImportError:
    thread = None

import colorama
import halo


# Log Level Representations
EMOJI_LOGLEVELS = {
    'CRITICAL': '{bg_red}{fg_white}¬_¬{style_reset_all}',
    'ERROR': '{fg_red}☒{style_reset_all}',
    'WARNING': '{fg_yellow}⚠{style_reset_all}',
    'INFO': '{fg_blue}ℹ{style_reset_all}',
    'DEBUG': '{fg_cyan}☐{style_reset_all}',
    'NOTSET': '{style_reset_all}¯\\_(o_o)_/¯'
}
EMOJI_LOGLEVELS['FATAL'] = EMOJI_LOGLEVELS['CRITICAL']
EMOJI_LOGLEVELS['WARN'] = EMOJI_LOGLEVELS['WARNING']


# ANSI Color setup
# Regex was gratefully borrowed from kfir on stackoverflow:
# https://stackoverflow.com/a/45448194
ansi_regex = r'\x1b(' \
             r'(\[\??\d+[hl])|' \
             r'([=<>a-kzNM78])|' \
             r'([\(\)][a-b0-2])|' \
             r'(\[\d{0,2}[ma-dgkjqi])|' \
             r'(\[\d+;\d+[hfy]?)|' \
             r'(\[;?[hf])|' \
             r'(#[3-68])|' \
             r'([01356]n)|' \
             r'(O[mlnp-z]?)|' \
             r'(/Z)|' \
             r'(\d+)|' \
             r'(\[\?\d;\d0c)|' \
             r'(\d;\dR))'
ansi_escape = re.compile(ansi_regex, flags=re.IGNORECASE)
ansi_colors = {}
for prefix, obj in (('fg', colorama.ansi.AnsiFore()),
                    ('bg', colorama.ansi.AnsiBack()),
                    ('style', colorama.ansi.AnsiStyle())):
    for color in [x for x in obj.__dict__ if not x.startswith('_')]:
        ansi_colors[prefix + '_' + color.lower()] = getattr(obj, color)


class ANSIFormatter(logging.Formatter):
    """A log formatter that inserts ANSI color.
    """
    def format(self, record):
        msg = super(ANSIFormatter, self).format(record)
        # Avoid .format() so we don't have to worry about the log content
        for color in ansi_colors:
            msg = msg.replace('{%s}' % color, ansi_colors[color])
        msg = msg + ansi_colors['style_reset_all']
        return msg


class ANSIEmojiLoglevelFormatter(ANSIFormatter):
    """A log formatter that makes the loglevel an emoji.
    """
    def format(self, record):
        record.levelname = EMOJI_LOGLEVELS[record.levelname].format(**ansi_colors)
        return super(ANSIEmojiLoglevelFormatter, self).format(record)


class ANSIStrippingFormatter(ANSIFormatter):
    """A log formatter that strips ANSI.
    """
    def format(self, record):
        msg = super(ANSIStrippingFormatter, self).format(record)
        return ansi_escape.sub('', msg)


class Configuration(object):
    """Represents the running configuration.

    This class never raises IndexError, instead it will return None if a
    section or option does not yet exist.
    """
    def __contains__(self, key):
        return self._config.__contains__(key)

    def __iter__(self):
        return self._config.__iter__()

    def __len__(self):
        return self._config.__len__()

    def __repr__(self):
        return self._config.__repr__()

    def keys(self):
        return self._config.keys()

    def items(self):
        return self._config.items()

    def values(self):
        return self._config.values()

    def __init__(self, *args, **kwargs):
        self._config = {}
        self.default_container = ConfigurationOption

    def __getitem__(self, key):
        """Returns a config section, creating it if it doesn't exist yet.
        """
        if key not in self._config:
            self.__dict__[key] = self._config[key] = ConfigurationOption()

        return self._config[key]

    def __setitem__(self, key, value):
        self.__dict__[key] = value
        self._config[key] = value

    def __delitem__(self, key):
        if key in self.__dict__ and key[0] != '_':
            del(self.__dict__[key])
        del(self._config[key])


class ConfigurationOption(Configuration):
    def __init__(self, *args, **kwargs):
        super(ConfigurationOption, self).__init__(*args, **kwargs)
        self.default_container = dict

    def __getitem__(self, key):
        """Returns a config section, creating it if it doesn't exist yet.
        """
        if key not in self._config:
            self.__dict__[key] = self._config[key] = None

        return self._config[key]


def handle_store_boolean(self, *args, **kwargs):
    """Does the add_argument for action='store_boolean'.
    """
    kwargs['add_dest'] = False
    disabled_args = None
    disabled_kwargs = kwargs.copy()
    disabled_kwargs['action'] = 'store_false'
    disabled_kwargs['help'] = 'Disable ' + kwargs['help']
    kwargs['action'] = 'store_true'
    kwargs['help'] = 'Enable ' + kwargs['help']

    for flag in args:
        if flag[:2] == '--':
            disabled_args = ('--no-' + flag[2:],)
            break

    self.add_argument(*args, **kwargs)
    self.add_argument(*disabled_args, **disabled_kwargs)

    return (args, kwargs, disabled_args, disabled_kwargs)


class SubparserWrapper(object):
    """Wrap subparsers so we can populate the normal and the shadow parser.
    """
    def __init__(self, cli, submodule, subparser):
        self.cli = cli
        self.submodule = submodule
        self.subparser = subparser

        for attr in dir(subparser):
            if not hasattr(self, attr):
                setattr(self, attr, getattr(subparser, attr))

    def add_argument(self, *args, **kwargs):
        if kwargs.get('add_dest', True):
            kwargs['dest'] = self.submodule + '_' + self.cli.get_argument_name(*args, **kwargs)
        if 'add_dest' in kwargs:
            del(kwargs['add_dest'])

        if 'action' in kwargs and kwargs['action'] == 'store_boolean':
            return handle_store_boolean(self, *args, **kwargs)

        self.subparser.add_argument(*args, **kwargs)

        if 'default' in kwargs:
            del(kwargs['default'])
        if 'action' in kwargs and kwargs['action'] == 'store_false':
            kwargs['action'] == 'store_true'
        self.cli.subcommands_default[self.submodule].add_argument(*args, **kwargs)


class CLIM(object):
    """# CLI Context Manager

    This class wraps some standard python modules in nice ways for CLI tools.
    It provides a Context Manager that can be used to quickly and easily
    write tools that behave in the way endusers expect. It's meant to lightly
    wrap standard python modules with just enough framework to make writing
    simple scripts simple while allowing you to organically grow into large
    complex programs with hundreds of options.

    ## Simple Example:

        cli = CLIM('My useful CLI tool.')

        @cli.argument('-n', '--name', help='Name to greet', default='World')
        @cli.entrypoint
        def main(cli):
            cli.log.info('Hello, %s!', cli.config.general.name)

        if __name__ == '__main__':
            with cli:
                cli.run()

    # Basics of a CLIM app

    Start by instaniating a CLIM context manager and defining your entrypoint:

        cli = CLIM('My useful CLI tool')

        def main(cli):
            print('Hello, %s!' % cli.config.general.name)

    From here, you should setup your CLI environment. I typically prefer to
    do this behind a __main__ check.

        if __name__ == '__main__':
            cli.entrypoint(main)
            cli.add_argument('-n', '--name', help='Name to greet', default='World')

    Finally, invoke it as a context manager and use `cli.run()` to dispatch to
    your entrypoint (or a subcommand, if one has been specified.)

            with cli:
                cli.run()

    ## Complete CLIM script, using functions

        cli = CLIM('My useful CLI tool')

        def main(cli):
            print('Hello, %s!' % cli.config.general.name)

        if __name__ == '__main__':
            cli.entrypoint(main)
            cli.add_argument('-n', '--name', help='Name to greet', default='World')

            with cli:
                cli.run()

    # Using decorators instead

    If you prefer you can use decorators instead. This can help as your program
    grows by keeping the definition of arguments near the relevant entrypoint.
    Not that due to the way decorators are evaluated you need to place all
    `@cli.argument()` decorators above all other decorators.

        cli = CLIM('My useful CLI tool')

        @cli.argument('-n', '--name', help='Name to greet', default='World')
        @cli.entrypoint
        def main(cli):
            print('Hello, %s!' % cli.config.general.name)

        if __name__ == '__main__':
            with cli:
                cli.run()

    # Using Subcommands

    A command pattern for CLI tools is to have subcommands. For example,
    you see this in git with `git status` and `git pull`. CLIM supports
    this pattern using the built-in argparse subcommand functionality.

    You can register subcommands by using the `cli.subcommand(func)`
    function, or by decorating functions with `@cli.subcommand`. In
    either case the subcommand name will be the same as the name of the
    function.

    You can access the underlying subcommand instance in two ways-

        * Attribute access (`cli.<subcommand>`)
        * Dictionary access (`cli.subcommands['<subcommand>']`)

    You should generally prefer the attribute access. If there is a conflict
    with an existing attribute or the name is not a legal attribute name you
    will have to access it via the dictionary.

    When subcommands are not in use `cli.run()` will always be the same as
    `cli.entrypoint()`. When subcommands are in use `cli.run()` will be
    pointed to the proper command to run. If no valid subcommand is given on
    the command line it will point to `cli.entrypoint()`. If a valid
    subcommand is supplied it will point to `<subcommand>()`.

    Note: Python 2 does not support calling @cli.entrypoint when subcommands
    are in use. If you need to call @cli.entrypoint when a subcommand is not
    specified you will need to use python 3.

    ## Subcommand Example

        cli = CLIM('My useful CLI tool with subcommands.')

        @cli.argument('-c', '--comma', help='Include the comma in output', default=True, action='store_true')
        @cli.entrypoint
        def main(cli):
            cli.log.info('Hello%s World!', cli.config.general.comma)

        @cli.argument('-n', '--name', help='Name to greet', default='World')
        @cli.subcommand
        def hello(cli):
            '''Description of hello subcommand here.'''
            cli.log.info('Hello%s %s!', cli.config.general.comma, cli.config.hello.name)

        def goodbye(cli):
            '''This will show up in --help output.'''
            cli.log.info('Goodbye%s %s!', cli.config.general.comma, cli.config.goodbye.name)

        if __name__ == '__main__':
            # You can register subcommands using decorators as seen above,
            # or using functions like like this:
            cli.subcommand(goodbye)
            cli.goodbye.add_argument('-n', '--name', help='Name to bid farewell to', default='World')

            with cli:
                cli.config.general.comma = ',' if cli.config.general.comma else ''
                cli.run()  # Automatically picks between main(), hello() and goodbye()

    # More Docs!

    Details about the rest of the system can be found in the [docs/](docs/) directory.
    """
    def __init__(self, description, entrypoint=None, fromfile_prefix_chars='@', conflict_handler='resolve', **kwargs):
        kwargs['fromfile_prefix_chars'] = fromfile_prefix_chars
        kwargs['conflict_handler'] = conflict_handler

        # Setup a lock for thread safety and hold it until initialization is complete
        self._lock = threading.RLock() if thread else None
        self.acquire_lock()

        # Define some basic info
        self._entrypoint = entrypoint
        self._inside_context_manager = False
        self._subparsers = None
        self._subparsers_default = None
        self.args = None
        self.ansi = ansi_colors
        self.config = Configuration()
        self.config_file = None
        self.prog_name = sys.argv[0][:-3] if sys.argv[0].endswith('.py') else sys.argv[0]
        self.subcommands = {}
        self.subcommands_default = {}
        self.spinner = halo.Halo
        self.version = 'unknown'

        # Initialize all the things
        self.initialize_argparse(description, kwargs)
        self.initialize_logging()

        # Release the lock
        self.release_lock()

    def initialize_argparse(self, description, kwargs):
        """Prepare to process arguments from sys.argv.
        """
        self._arg_defaults = argparse.ArgumentParser(description=description, **kwargs)
        self._arg_parser = argparse.ArgumentParser(description=description, **kwargs)
        self.set_defaults = self._arg_parser.set_defaults
        self.print_usage = self._arg_parser.print_usage
        self.print_help = self._arg_parser.print_help

    def add_argument(self, *args, **kwargs):
        """Wrapper to add arguments to both the main and the shadow argparser.
        """
        if kwargs.get('add_dest', True):
            kwargs['dest'] = 'general_' + self.get_argument_name(*args, **kwargs)
        if 'add_dest' in kwargs:
            del(kwargs['add_dest'])

        if 'action' in kwargs and kwargs['action'] == 'store_boolean':
            return handle_store_boolean(self, *args, **kwargs)

        self._arg_parser.add_argument(*args, **kwargs)

        # Populate the shadow parser
        if 'default' in kwargs:
            del(kwargs['default'])
        if 'action' in kwargs and kwargs['action'] == 'store_false':
            kwargs['action'] == 'store_true'
        self._arg_defaults.add_argument(*args, **kwargs)

    def initialize_logging(self):
        """Prepare the defaults for the logging infrastructure.
        """
        self.log_file = None
        self.log_file_mode = 'a'
        self.log_file_handler = None
        self.log_print = True
        self.log_print_to = sys.stderr
        self.log_print_level = logging.INFO
        self.log_file_level = logging.DEBUG
        self.log_level = logging.INFO
        self.log = logging.getLogger(self.__class__.__name__)
        self.log.setLevel(logging.DEBUG)
        logging.root.setLevel(logging.DEBUG)
        self.add_argument('-V', '--version', version=self.version, action='version', help='Display the version and exit')
        self.add_argument('-v', '--verbose', action='store_true', help='Make the logging more verbose')
        self.add_argument('--datetime-fmt', default='%Y-%m-%d %H:%M:%S', help='Format string for datetimes')
        self.add_argument('--log-fmt', default='%(levelname)s %(message)s', help='Format string for printed log output')
        self.add_argument('--log-file-fmt', default='[%(levelname)s] [%(asctime)s] [file:%(pathname)s] [line:%(lineno)d] %(message)s', help='Format string for log file.')
        self.add_argument('--log-file', help='File to write log messages to')
        self.add_argument('--color', action='store_boolean', default=True, help='color in output')
        self.add_argument('-c', '--config-file', help='The config file to read and/or write')
        self.add_argument('--save-config', action='store_true', help='Save the running configuration to the config file')

    def add_subparsers(self, title='Sub-commands', **kwargs):
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        self.acquire_lock()
        self._subparsers_default = self._arg_defaults.add_subparsers(title=title, dest='subparsers', **kwargs)
        self._subparsers = self._arg_parser.add_subparsers(title=title, dest='subparsers', **kwargs)
        self.release_lock()

    def acquire_lock(self):
        """Acquire the CLIM lock for exclusive access to properties.
        """
        if self._lock:
            self._lock.acquire()

    def release_lock(self):
        """Release the CLIM lock.
        """
        if self._lock:
            self._lock.release()

    def find_config_file(self):
        """Locate the config file.
        """
        if self.config_file:
            return self.config_file

        if self.args and self.args.general_config_file:
            return self.args.general_config_file

        return os.path.abspath(os.path.expanduser('~/.%s.ini' % self.prog_name))

    def get_argument_name(self, *args, **kwargs):
        """Takes argparse arguments and returns the dest name.
        """
        return self._arg_parser._get_optional_kwargs(*args, **kwargs)['dest']

    def argument(self, *args, **kwargs):
        """Decorator to call self.add_argument or self.<subcommand>.add_argument.
        """
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        def argument_function(handler):
            if handler is self._entrypoint:
                self.add_argument(*args, **kwargs)

            elif handler.__name__ in self.subcommands:
                self.subcommands[handler.__name__].add_argument(*args, **kwargs)

            else:
                raise RuntimeError('Decorated function is not entrypoint or subcommand!')

            return handler

        return argument_function

    def arg_passed(self, arg):
        """Returns True if arg was passed on the command line.
        """
        return self.args_passed[arg] in (None, False)

    def parse_args(self):
        """Parse the CLI args.
        """
        if self.args:
            self.log.debug('Warning: Arguments have already been parsed, ignoring duplicate attempt!')
            return

        self.acquire_lock()

        self.args_passed = self._arg_defaults.parse_args()
        self.args = self._arg_parser.parse_args()

        if 'entrypoint' in self.args:
            self._entrypoint = self.args.entrypoint

        if self.args.general_config_file:
            self.config_file = self.args.general_config_file

        self.release_lock()

    def read_config(self):
        """Parse the configuration file and determine the runtime configuration.
        """
        self.acquire_lock()
        self.config_file = self.find_config_file()

        if self.config_file and os.path.exists(cli.config_file):
            config = RawConfigParser(self.config)
            config.read(self.config_file)

            # Iterate over the config file options and write them into self.config
            for section in config.sections():
                for option in config.options(section):
                    value = config.get(section, option)

                    # Coerce values into useful datatypes
                    if value.lower() in ['1', 'yes', 'true', 'on']:
                        value = True
                    elif value.lower() in ['0', 'no', 'false', 'none', 'off']:
                        value = False
                    elif value.replace('.', '').isdigit():
                        if '.' in value:
                            value = Decimal(value)
                        else:
                            value = int(value)

                    self.config[section][option] = value

        # Fold the CLI args into self.config
        for argument in vars(self.args):
            if argument in ('subparsers', 'entrypoint'):
                continue

            if '_' not in argument:
                continue

            section, option = argument.split('_', 1)
            if hasattr(self.args_passed, argument):
                self.config[section][option] = getattr(self.args, argument)
            else:
                if option not in self.config[section]:
                    self.config[section][option] = getattr(self.args, argument)

        self.release_lock()

    def save_config(self):
        """Save the current configuration to the config file.
        """
        self.log.debug("Saving config file to '%s'", self.config_file)

        if not self.config_file:
            self.log.warning('%s.config_file file not set, not saving config!', self.__class__.__name__)
            return

        self.acquire_lock()

        config = RawConfigParser()
        for section_name, section in self.config._config.items():
            config.add_section(section_name)
            for option_name, value in section.items():
                if section_name == 'general':
                    if option_name in ['save_config']:
                        continue
                config.set(section_name, option_name, str(value))

        with NamedTemporaryFile(mode='w', dir=os.path.dirname(self.config_file), delete=False) as tmpfile:
            config.write(tmpfile)

        # Move the new config file into place atomically
        if os.path.getsize(tmpfile.name) > 0:
            os.rename(tmpfile.name, self.config_file)
        else:
            self.log.warning('Config file saving failed, not replacing %s with %s.', self.config_file, tmpfile.name)

        self.release_lock()

    def run(self):
        """Execute the entrypoint function.
        """
        if not self._inside_context_manager:
            self.__enter__()
            self.log.debug('Warning: self.run() called outside of context manager. This will preclude calling self.__exit__().')

        if not self._entrypoint:
            raise RuntimeError('No entrypoint provided!')

        return self._entrypoint(self)

    def entrypoint(self, handler):
        """Set the entrypoint for when no subcommand is provided.
        """
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        self.acquire_lock()
        self._entrypoint = handler
        self.release_lock()

        return handler

    def subcommand(self, handler, name=None, **kwargs):
        """Register a subcommand.

        If name is not provided we use `handler.__name__`.
        """
        if self._inside_context_manager:
            raise RuntimeError('You must run this before the with statement!')

        if self._subparsers is None:
            self.add_subparsers()

        self.acquire_lock()

        if not name:
            name = handler.__name__

        kwargs['help'] = handler.__doc__.split('\n')[0] if handler.__doc__ else None
        self.subcommands_default[name] = self._subparsers_default.add_parser(name, **kwargs)
        self.subcommands[name] = SubparserWrapper(self, name, self._subparsers.add_parser(name, **kwargs))
        self.subcommands[name].set_defaults(entrypoint=handler)

        if name not in self.__dict__:
            self.__dict__[name] = self.subcommands[name]
        else:
            self.log.debug("Could not add subcommand '%s' to attributes, key already exists!", name)

        self.release_lock()

        return handler

    def setup_logging(self):
        """Called by __enter__() to setup the logging configuration.
        """
        if len(logging.root.handlers) != 0:
            # This is not a design decision. This is what I'm doing for now until I can examine and think about this situation in more detail.
            raise RuntimeError('CLIM should be the only system installing root log handlers!')

        self.acquire_lock()

        if self.config['general']['verbose']:
            self.log_print_level = logging.DEBUG

        self.log_file = self.config['general']['log_file'] or self.log_file
        self.log_file_format = self.config['general']['log_file_fmt']
        self.log_file_format = ANSIStrippingFormatter(self.config['general']['log_file_fmt'], self.config['general']['datetime_fmt'])
        self.log_format = self.config['general']['log_fmt']

        if self.config.general.color:
            self.log_format = ANSIEmojiLoglevelFormatter(self.args.general_log_fmt, self.config.general.datetime_fmt)
        else:
            self.log_format = ANSIStrippingFormatter(self.args.general_log_fmt, self.config.general.datetime_fmt)

        if self.log_file:
            self.log_file_handler = logging.FileHandler(self.log_file, self.log_file_mode)
            self.log_file_handler.setLevel(self.log_file_level)
            self.log_file_handler.setFormatter(self.log_file_format)
            logging.root.addHandler(self.log_file_handler)

        if self.log_print:
            self.log_print_handler = logging.StreamHandler(self.log_print_to)
            self.log_print_handler.setLevel(self.log_print_level)
            self.log_print_handler.setFormatter(self.log_format)
            logging.root.addHandler(self.log_print_handler)

        self.release_lock()

    def __enter__(self):
        if self._inside_context_manager:
            self.log.debug('Warning: context manager was entered again. This usually means that self.run() was called before the with statement. You probably do not want to do that.')
            return

        self.acquire_lock()
        self._inside_context_manager = True
        self.release_lock()

        colorama.init()
        self.parse_args()
        self.read_config()
        self.setup_logging()

        if self.config.general.save_config:
            self.save_config()

        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.acquire_lock()
        self._inside_context_manager = False
        self.release_lock()

        if exc_type is not None:
            logging.exception(exc_val)
            exit(255)


if __name__ == '__main__':
        cli = CLIM('My useful CLI tool with subcommands.')

        @cli.argument('-c', '--comma', help='comma in output', default=True, action='store_boolean')
        @cli.entrypoint
        def main(cli):
            comma = ',' if cli.config.general.comma else ''
            cli.log.info('{bg_green}{fg_red}Hello%s World!', comma)

        @cli.argument('-n', '--name', help='Name to greet', default='World')
        @cli.subcommand
        def hello(cli):
            '''Description of hello subcommand here.'''
            comma = ',' if cli.config.general.comma else ''
            cli.log.info('{fg_blue}Hello%s %s!', comma, cli.config.hello.name)

        def goodbye(cli):
            '''This will show up in --help output.'''
            comma = ',' if cli.config.general.comma else ''
            cli.log.info('{bg_red}Goodbye%s %s!', comma, cli.config.goodbye.name)

        @cli.argument('-n', '--name', help='Name to greet', default='World')
        @cli.subcommand
        def thinking(cli):
            '''Think a bit before greeting the user.'''
            comma = ',' if cli.config.general.comma else ''
            spinner = cli.spinner(text='Just a moment...', spinner='earth')
            spinner.start()
            sleep(2)
            spinner.stop()

            with cli.spinner(text='Almost there!', spinner='moon'):
                sleep(2)

            cli.log.info('{fg_cyan}Hello%s %s!', comma, cli.config.thinking.name)

        if __name__ == '__main__':
            # You can register subcommands using decorators as seen above,
            # or using functions like like this:
            cli.subcommand(goodbye)
            cli.goodbye.add_argument('-n', '--name', help='Name to bid farewell to', default='World')

            with cli:
                cli.run()  # Automatically picks between main(), hello() and goodbye()
