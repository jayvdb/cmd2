#!/usr/bin/env python
# coding=utf-8
"""Variant on standard library's cmd with extra features.

To use, simply import cmd2.Cmd instead of cmd.Cmd; use precisely as though you
were using the standard library's cmd, while enjoying the extra features.

Searchable command history (commands: "history")
Run commands from file, save to file, edit commands in file
Multi-line commands
Special-character shortcut commands (beyond cmd's "?" and "!")
Settable environment parameters
Parsing commands with `argparse` argument parsers (flags)
Redirection to file or paste buffer (clipboard) with > or >>
Easy transcript-based testing of applications (see examples/example.py)
Bash-style ``select`` available

Note that redirection with > and | will only work if `self.poutput()`
is used in place of `print`.

- Catherine Devlin, Jan 03 2008 - catherinedevlin.blogspot.com

Git repository on GitHub at https://github.com/python-cmd2/cmd2
"""
# This module has many imports, quite a few of which are only
# infrequently utilized. To reduce the initial overhead of
# import this module, many of these imports are lazy-loaded
# i.e. we only import the module when we use it
# For example, we don't import the 'traceback' module
# until the pexcept() function is called and the debug
# setting is True
import argparse
import cmd
import glob
import inspect
import os
import pickle
import re
import sys
import threading
from code import InteractiveConsole
from collections import namedtuple
from contextlib import redirect_stdout
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple, Type, Union

from . import Cmd2ArgumentParser, CompletionItem
from . import ansi
from . import constants
from . import plugin
from . import utils
from .clipboard import can_clip, get_paste_buffer, write_to_paste_buffer
from .history import History, HistoryItem
from .parsing import StatementParser, Statement, Macro, MacroArg, shlex_split

# Set up readline
from .rl_utils import rl_type, RlType, rl_get_point, rl_set_prompt, vt100_support, rl_make_safe_prompt

if rl_type == RlType.NONE:  # pragma: no cover
    rl_warning = "Readline features including tab completion have been disabled since no \n" \
                 "supported version of readline was found. To resolve this, install \n" \
                 "pyreadline on Windows or gnureadline on Mac.\n\n"
    sys.stderr.write(ansi.style_warning(rl_warning))
else:
    from .rl_utils import rl_force_redisplay, readline

    # Used by rlcompleter in Python console loaded by py command
    orig_rl_delims = readline.get_completer_delims()

    if rl_type == RlType.PYREADLINE:

        # Save the original pyreadline display completion function since we need to override it and restore it
        # noinspection PyProtectedMember,PyUnresolvedReferences
        orig_pyreadline_display = readline.rl.mode._display_completions

    elif rl_type == RlType.GNU:

        # Get the readline lib so we can make changes to it
        import ctypes
        from .rl_utils import readline_lib

        rl_basic_quote_characters = ctypes.c_char_p.in_dll(readline_lib, "rl_basic_quote_characters")
        orig_rl_basic_quotes = ctypes.cast(rl_basic_quote_characters, ctypes.c_void_p).value

# Detect whether IPython is installed to determine if the built-in "ipy" command should be included
ipython_available = True
try:
    # noinspection PyUnresolvedReferences,PyPackageRequirements
    from IPython import embed
except ImportError:  # pragma: no cover
    ipython_available = False

INTERNAL_COMMAND_EPILOG = ("Notes:\n"
                           "  This command is for internal use and is not intended to be called from the\n"
                           "  command line.")

# All command functions start with this
COMMAND_FUNC_PREFIX = 'do_'

# All help functions start with this
HELP_FUNC_PREFIX = 'help_'

# All command completer functions start with this
COMPLETER_FUNC_PREFIX = 'complete_'

# Sorting keys for strings
ALPHABETICAL_SORT_KEY = utils.norm_fold
NATURAL_SORT_KEY = utils.natural_keys

# Used as the command name placeholder in disabled command messages.
COMMAND_NAME = "<COMMAND_NAME>"

############################################################################################################
# The following are optional attributes added to do_* command functions
############################################################################################################

# The custom help category a command belongs to
CMD_ATTR_HELP_CATEGORY = 'help_category'

# The argparse parser for the command
CMD_ATTR_ARGPARSER = 'argparser'


def categorize(func: Union[Callable, Iterable[Callable]], category: str) -> None:
    """Categorize a function.

    The help command output will group this function under the specified category heading

    :param func: function or list of functions to categorize
    :param category: category to put it in
    """
    if isinstance(func, Iterable):
        for item in func:
            setattr(item, CMD_ATTR_HELP_CATEGORY, category)
    else:
        setattr(func, CMD_ATTR_HELP_CATEGORY, category)


def with_category(category: str) -> Callable:
    """A decorator to apply a category to a command function."""
    def cat_decorator(func):
        categorize(func, category)
        return func
    return cat_decorator


def with_argument_list(*args: List[Callable], preserve_quotes: bool = False) -> Callable[[List], Optional[bool]]:
    """A decorator to alter the arguments passed to a do_* cmd2 method. Default passes a string of whatever the user
    typed. With this decorator, the decorated method will receive a list of arguments parsed from user input.

    :param args: Single-element positional argument list containing do_* method this decorator is wrapping
    :param preserve_quotes: if True, then argument quotes will not be stripped
    :return: function that gets passed a list of argument strings
    """
    import functools

    def arg_decorator(func: Callable):
        @functools.wraps(func)
        def cmd_wrapper(cmd2_app, statement: Union[Statement, str]):
            _, parsed_arglist = cmd2_app.statement_parser.get_command_arg_list(command_name,
                                                                               statement,
                                                                               preserve_quotes)

            return func(cmd2_app, parsed_arglist)

        command_name = func.__name__[len(COMMAND_FUNC_PREFIX):]
        cmd_wrapper.__doc__ = func.__doc__
        return cmd_wrapper

    if len(args) == 1 and callable(args[0]):
        # noinspection PyTypeChecker
        return arg_decorator(args[0])
    else:
        # noinspection PyTypeChecker
        return arg_decorator


def with_argparser_and_unknown_args(argparser: argparse.ArgumentParser, *,
                                    ns_provider: Optional[Callable[..., argparse.Namespace]] = None,
                                    preserve_quotes: bool = False) -> \
        Callable[[argparse.Namespace, List], Optional[bool]]:
    """A decorator to alter a cmd2 method to populate its ``args`` argument by parsing arguments with the given
    instance of argparse.ArgumentParser, but also returning unknown args as a list.

    :param argparser: unique instance of ArgumentParser
    :param ns_provider: An optional function that accepts a cmd2.Cmd object as an argument and returns an
                        argparse.Namespace. This is useful if the Namespace needs to be prepopulated with
                        state data that affects parsing.
    :param preserve_quotes: if True, then arguments passed to argparse maintain their quotes
    :return: function that gets passed argparse-parsed args in a Namespace and a list of unknown argument strings
             A member called __statement__ is added to the Namespace to provide command functions access to the
             Statement object. This can be useful if the command function needs to know the command line.

    """
    import functools

    def arg_decorator(func: Callable):
        @functools.wraps(func)
        def cmd_wrapper(cmd2_app, statement: Union[Statement, str]):
            statement, parsed_arglist = cmd2_app.statement_parser.get_command_arg_list(command_name,
                                                                                       statement,
                                                                                       preserve_quotes)

            if ns_provider is None:
                namespace = None
            else:
                namespace = ns_provider(cmd2_app)

            try:
                args, unknown = argparser.parse_known_args(parsed_arglist, namespace)
            except SystemExit:
                return
            else:
                setattr(args, '__statement__', statement)
                return func(cmd2_app, args, unknown)

        # argparser defaults the program name to sys.argv[0]
        # we want it to be the name of our command
        command_name = func.__name__[len(COMMAND_FUNC_PREFIX):]
        argparser.prog = command_name

        # If the description has not been set, then use the method docstring if one exists
        if argparser.description is None and func.__doc__:
            argparser.description = func.__doc__

        # Set the command's help text as argparser.description (which can be None)
        cmd_wrapper.__doc__ = argparser.description

        # Mark this function as having an argparse ArgumentParser
        setattr(cmd_wrapper, CMD_ATTR_ARGPARSER, argparser)

        return cmd_wrapper

    # noinspection PyTypeChecker
    return arg_decorator


def with_argparser(argparser: argparse.ArgumentParser, *,
                   ns_provider: Optional[Callable[..., argparse.Namespace]] = None,
                   preserve_quotes: bool = False) -> Callable[[argparse.Namespace], Optional[bool]]:
    """A decorator to alter a cmd2 method to populate its ``args`` argument by parsing arguments
    with the given instance of argparse.ArgumentParser.

    :param argparser: unique instance of ArgumentParser
    :param ns_provider: An optional function that accepts a cmd2.Cmd object as an argument and returns an
                        argparse.Namespace. This is useful if the Namespace needs to be prepopulated with
                        state data that affects parsing.
    :param preserve_quotes: if True, then arguments passed to argparse maintain their quotes
    :return: function that gets passed the argparse-parsed args in a Namespace
             A member called __statement__ is added to the Namespace to provide command functions access to the
             Statement object. This can be useful if the command function needs to know the command line.
    """
    import functools

    def arg_decorator(func: Callable):
        @functools.wraps(func)
        def cmd_wrapper(cmd2_app, statement: Union[Statement, str]):
            statement, parsed_arglist = cmd2_app.statement_parser.get_command_arg_list(command_name,
                                                                                       statement,
                                                                                       preserve_quotes)

            if ns_provider is None:
                namespace = None
            else:
                namespace = ns_provider(cmd2_app)

            try:
                args = argparser.parse_args(parsed_arglist, namespace)
            except SystemExit:
                return
            else:
                setattr(args, '__statement__', statement)
                return func(cmd2_app, args)

        # argparser defaults the program name to sys.argv[0]
        # we want it to be the name of our command
        command_name = func.__name__[len(COMMAND_FUNC_PREFIX):]
        argparser.prog = command_name

        # If the description has not been set, then use the method docstring if one exists
        if argparser.description is None and func.__doc__:
            argparser.description = func.__doc__

        # Set the command's help text as argparser.description (which can be None)
        cmd_wrapper.__doc__ = argparser.description

        # Mark this function as having an argparse ArgumentParser
        setattr(cmd_wrapper, CMD_ATTR_ARGPARSER, argparser)

        return cmd_wrapper

    # noinspection PyTypeChecker
    return arg_decorator


class _SavedReadlineSettings:
    """readline settings that are backed up when switching between readline environments"""
    def __init__(self):
        self.completer = None
        self.delims = ''
        self.basic_quotes = None


class _SavedCmd2Env:
    """cmd2 environment settings that are backed up when entering an interactive Python shell"""
    def __init__(self):
        self.readline_settings = _SavedReadlineSettings()
        self.readline_module = None
        self.history = []
        self.sys_stdout = None
        self.sys_stdin = None


class EmbeddedConsoleExit(SystemExit):
    """Custom exception class for use with the py command."""
    pass


class EmptyStatement(Exception):
    """Custom exception class for handling behavior when the user just presses <Enter>."""
    pass


# Contains data about a disabled command which is used to restore its original functions when the command is enabled
DisabledCommand = namedtuple('DisabledCommand', ['command_function', 'help_function', 'completer_function'])


class Cmd(cmd.Cmd):
    """An easy but powerful framework for writing line-oriented command interpreters.

    Extends the Python Standard Library’s cmd package by adding a lot of useful features
    to the out of the box configuration.

    Line-oriented command interpreters are often useful for test harnesses, internal tools, and rapid prototypes.
    """
    DEFAULT_EDITOR = utils.find_editor()

    def __init__(self, completekey: str = 'tab', stdin=None, stdout=None, *,
                 persistent_history_file: str = '', persistent_history_length: int = 1000,
                 startup_script: Optional[str] = None, use_ipython: bool = False,
                 allow_cli_args: bool = True, transcript_files: Optional[List[str]] = None,
                 allow_redirection: bool = True, multiline_commands: Optional[List[str]] = None,
                 terminators: Optional[List[str]] = None, shortcuts: Optional[Dict[str, str]] = None) -> None:
        """An easy but powerful framework for writing line-oriented command interpreters, extends Python's cmd package.

        :param completekey: readline name of a completion key, default to Tab
        :param stdin: alternate input file object, if not specified, sys.stdin is used
        :param stdout: alternate output file object, if not specified, sys.stdout is used
        :param persistent_history_file: file path to load a persistent cmd2 command history from
        :param persistent_history_length: max number of history items to write to the persistent history file
        :param startup_script: file path to a script to execute at startup
        :param use_ipython: should the "ipy" command be included for an embedded IPython shell
        :param allow_cli_args: if True, then cmd2 will process command line arguments as either
                               commands to be run or, if -t is specified, transcript files to run.
                               This should be set to False if your application parses its own arguments.
        :param transcript_files: allow running transcript tests when allow_cli_args is False
        :param allow_redirection: should output redirection and pipes be allowed. this is only a security setting
                                  and does not alter parsing behavior.
        :param multiline_commands: list of commands allowed to accept multi-line input
        :param terminators: list of characters that terminate a command. These are mainly intended for terminating
                            multiline commands, but will also terminate single-line commands. If not supplied, then
                            defaults to semicolon. If your app only contains single-line commands and you want
                            terminators to be treated as literals by the parser, then set this to an empty list.
        :param shortcuts: dictionary containing shortcuts for commands. If not supplied, then defaults to
                          constants.DEFAULT_SHORTCUTS.
        """
        # If use_ipython is False, make sure the do_ipy() method doesn't exit
        if not use_ipython:
            try:
                del Cmd.do_ipy
            except AttributeError:
                pass

        # initialize plugin system
        # needs to be done before we call __init__(0)
        self._initialize_plugin_system()

        # Call super class constructor
        super().__init__(completekey=completekey, stdin=stdin, stdout=stdout)

        # Attributes which should NOT be dynamically settable via the set command at runtime
        # To prevent a user from altering these with the py/ipy commands, remove locals_in_py from the
        # settable dictionary during your applications's __init__ method.
        self.default_to_shell = False  # Attempt to run unrecognized commands as shell commands
        self.quit_on_sigint = False  # Quit the loop on interrupt instead of just resetting prompt
        self.allow_redirection = allow_redirection  # Security setting to prevent redirection of stdout

        # Attributes which ARE dynamically settable via the set command at runtime
        self.continuation_prompt = '> '
        self.debug = False
        self.echo = False
        self.editor = self.DEFAULT_EDITOR
        self.feedback_to_output = False  # Do not include nonessentials in >, | output by default (things like timing)
        self.locals_in_py = False

        # The maximum number of CompletionItems to display during tab completion. If the number of completion
        # suggestions exceeds this number, they will be displayed in the typical columnized format and will
        # not include the description value of the CompletionItems.
        self.max_completion_items = 50

        self.quiet = False  # Do not suppress nonessential output
        self.timing = False  # Prints elapsed time for each command

        # To make an attribute settable with the "do_set" command, add it to this ...
        self.settable = \
            {
                # allow_ansi is a special case in which it's an application-wide setting defined in ansi.py
                'allow_ansi': ('Allow ANSI escape sequences in output '
                               '(valid values: {}, {}, {})'.format(ansi.ANSI_TERMINAL,
                                                                   ansi.ANSI_ALWAYS,
                                                                   ansi.ANSI_NEVER)),
                'continuation_prompt': 'On 2nd+ line of input',
                'debug': 'Show full error stack on error',
                'echo': 'Echo command issued into output',
                'editor': 'Program used by ``edit``',
                'feedback_to_output': 'Include nonessentials in `|`, `>` results',
                'locals_in_py': 'Allow access to your application in py via self',
                'max_completion_items': 'Maximum number of CompletionItems to display during tab completion',
                'prompt': 'The prompt issued to solicit input',
                'quiet': "Don't print nonessential feedback",
                'timing': 'Report execution times'
            }

        # Commands to exclude from the help menu and tab completion
        self.hidden_commands = ['eof', '_relative_load', '_relative_run_script']

        # Initialize history
        self._persistent_history_length = persistent_history_length
        self._initialize_history(persistent_history_file)

        # Commands to exclude from the history command
        self.exclude_from_history = '''history edit eof'''.split()

        # Dictionary of macro names and their values
        self.macros = dict()

        # Keeps track of typed command history in the Python shell
        self._py_history = []

        # The name by which Python environments refer to the PyBridge to call app commands
        self.py_bridge_name = 'app'

        # Defines app-specific variables/functions available in Python shells and pyscripts
        self.py_locals = dict()

        # True if running inside a Python script or interactive console, False otherwise
        self._in_py = False

        self.statement_parser = StatementParser(terminators=terminators,
                                                multiline_commands=multiline_commands,
                                                shortcuts=shortcuts)

        # Verify commands don't have invalid names (like starting with a shortcut)
        for cur_cmd in self.get_all_commands():
            valid, errmsg = self.statement_parser.is_valid_command(cur_cmd)
            if not valid:
                raise ValueError("Invalid command name {!r}: {}".format(cur_cmd, errmsg))

        # Stores results from the last command run to enable usage of results in a Python script or interactive console
        # Built-in commands don't make use of this.  It is purely there for user-defined commands and convenience.
        self.last_result = None

        # Used by run_script command to store current script dir as a LIFO queue to support _relative_run_script command
        self._script_dir = []

        # Context manager used to protect critical sections in the main thread from stopping due to a KeyboardInterrupt
        self.sigint_protection = utils.ContextFlag()

        # If the current command created a process to pipe to, then this will be a ProcReader object.
        # Otherwise it will be None. Its used to know when a pipe process can be killed and/or waited upon.
        self._cur_pipe_proc_reader = None

        # Used by complete() for readline tab completion
        self.completion_matches = []

        # Used to keep track of whether we are redirecting or piping output
        self._redirecting = False

        # Used to keep track of whether a continuation prompt is being displayed
        self._at_continuation_prompt = False

        # The multiline command currently being typed which is used to tab complete multiline commands.
        self._multiline_in_progress = ''

        # The error that prints when no help information can be found
        self.help_error = "No help on {}"

        # The error that prints when a non-existent command is run
        self.default_error = "{} is not a recognized command, alias, or macro"

        # If this string is non-empty, then this warning message will print if a broken pipe error occurs while printing
        self.broken_pipe_warning = ''

        # Commands that will run at the beginning of the command loop
        self._startup_commands = []

        # If a startup script is provided, then execute it in the startup commands
        if startup_script is not None:
            startup_script = os.path.abspath(os.path.expanduser(startup_script))
            if os.path.exists(startup_script) and os.path.getsize(startup_script) > 0:
                self._startup_commands.append("run_script '{}'".format(startup_script))

        # Transcript files to run instead of interactive command loop
        self._transcript_files = None

        # Check for command line args
        if allow_cli_args:
            parser = argparse.ArgumentParser()
            parser.add_argument('-t', '--test', action="store_true",
                                help='Test against transcript(s) in FILE (wildcards OK)')
            callopts, callargs = parser.parse_known_args()

            # If transcript testing was called for, use other arguments as transcript files
            if callopts.test:
                self._transcript_files = callargs
            # If commands were supplied at invocation, then add them to the command queue
            elif callargs:
                self._startup_commands.extend(callargs)
        elif transcript_files:
            self._transcript_files = transcript_files

        # The default key for sorting string results. Its default value performs a case-insensitive alphabetical sort.
        # If natural sorting is preferred, then set this to NATURAL_SORT_KEY.
        # cmd2 uses this key for sorting:
        #     command and category names
        #     alias, macro, settable, and shortcut names
        #     tab completion results when self.matches_sorted is False
        self.default_sort_key = ALPHABETICAL_SORT_KEY

        ############################################################################################################
        # The following variables are used by tab-completion functions. They are reset each time complete() is run
        # in reset_completion_defaults() and it is up to completer functions to set them before returning results.
        ############################################################################################################

        # If True and a single match is returned to complete(), then a space will be appended
        # if the match appears at the end of the line
        self.allow_appended_space = True

        # If True and a single match is returned to complete(), then a closing quote
        # will be added if there is an unmatched opening quote
        self.allow_closing_quote = True

        # An optional header that prints above the tab-completion suggestions
        self.completion_header = ''

        # Use this list if you are completing strings that contain a common delimiter and you only want to
        # display the final portion of the matches as the tab-completion suggestions. The full matches
        # still must be returned from your completer function. For an example, look at path_complete()
        # which uses this to show only the basename of paths as the suggestions. delimiter_complete() also
        # populates this list.
        self.display_matches = []

        # Used by functions like path_complete() and delimiter_complete() to properly
        # quote matches that are completed in a delimited fashion
        self.matches_delimited = False

        # Set to True before returning matches to complete() in cases where matches have already been sorted.
        # If False, then complete() will sort the matches using self.default_sort_key before they are displayed.
        self.matches_sorted = False

        # Set the pager(s) for use with the ppaged() method for displaying output using a pager
        if sys.platform.startswith('win'):
            self.pager = self.pager_chop = 'more'
        else:
            # Here is the meaning of the various flags we are using with the less command:
            # -S causes lines longer than the screen width to be chopped (truncated) rather than wrapped
            # -R causes ANSI "color" escape sequences to be output in raw form (i.e. colors are displayed)
            # -X disables sending the termcap initialization and deinitialization strings to the terminal
            # -F causes less to automatically exit if the entire file can be displayed on the first screen
            self.pager = 'less -RXF'
            self.pager_chop = 'less -SRXF'

        # This boolean flag determines whether or not the cmd2 application can interact with the clipboard
        self._can_clip = can_clip

        # This determines the value returned by cmdloop() when exiting the application
        self.exit_code = 0

        # This lock should be acquired before doing any asynchronous changes to the terminal to
        # ensure the updates to the terminal don't interfere with the input being typed or output
        # being printed by a command.
        self.terminal_lock = threading.RLock()

        # Commands that have been disabled from use. This is to support commands that are only available
        # during specific states of the application. This dictionary's keys are the command names and its
        # values are DisabledCommand objects.
        self.disabled_commands = dict()

        # If any command has been categorized, then all other commands that haven't been categorized
        # will display under this section in the help output.
        self.default_category = 'Uncategorized'

    # -----  Methods related to presenting output to the user -----

    @property
    def allow_ansi(self) -> str:
        """Read-only property needed to support do_set when it reads allow_ansi"""
        return ansi.allow_ansi

    @allow_ansi.setter
    def allow_ansi(self, new_val: str) -> None:
        """Setter property needed to support do_set when it updates allow_ansi"""
        new_val = new_val.lower()
        if new_val == ansi.ANSI_TERMINAL.lower():
            ansi.allow_ansi = ansi.ANSI_TERMINAL
        elif new_val == ansi.ANSI_ALWAYS.lower():
            ansi.allow_ansi = ansi.ANSI_ALWAYS
        elif new_val == ansi.ANSI_NEVER.lower():
            ansi.allow_ansi = ansi.ANSI_NEVER
        else:
            self.perror('Invalid value: {} (valid values: {}, {}, {})'.format(new_val, ansi.ANSI_TERMINAL,
                                                                              ansi.ANSI_ALWAYS, ansi.ANSI_NEVER))

    @property
    def visible_prompt(self) -> str:
        """Read-only property to get the visible prompt with any ANSI escape codes stripped.

        Used by transcript testing to make it easier and more reliable when users are doing things like coloring the
        prompt using ANSI color codes.

        :return: prompt stripped of any ANSI escape codes
        """
        return ansi.strip_ansi(self.prompt)

    @property
    def aliases(self) -> Dict[str, str]:
        """Read-only property to access the aliases stored in the StatementParser."""
        return self.statement_parser.aliases

    def poutput(self, msg: Any, *, end: str = '\n') -> None:
        """Print message to self.stdout and appends a newline by default

        Also handles BrokenPipeError exceptions for when a commands's output has
        been piped to another process and that process terminates before the
        cmd2 command is finished executing.

        :param msg: message to print (anything convertible to a str with '{}'.format() is OK)
        :param end: string appended after the end of the message, default a newline
        """
        try:
            ansi.ansi_aware_write(self.stdout, "{}{}".format(msg, end))
        except BrokenPipeError:
            # This occurs if a command's output is being piped to another
            # process and that process closes before the command is
            # finished. If you would like your application to print a
            # warning message, then set the broken_pipe_warning attribute
            # to the message you want printed.
            if self.broken_pipe_warning:
                sys.stderr.write(self.broken_pipe_warning)

    @staticmethod
    def perror(msg: Any, *, end: str = '\n', apply_style: bool = True) -> None:
        """Print message to sys.stderr

        :param msg: message to print (anything convertible to a str with '{}'.format() is OK)
        :param end: string appended after the end of the message, default a newline
        :param apply_style: If True, then ansi.style_error will be applied to the message text. Set to False in cases
                            where the message text already has the desired style. Defaults to True.
        """
        if apply_style:
            final_msg = ansi.style_error(msg)
        else:
            final_msg = "{}".format(msg)
        ansi.ansi_aware_write(sys.stderr, final_msg + end)

    def pwarning(self, msg: Any, *, end: str = '\n') -> None:
        """Apply the warning style to a message and print it to sys.stderr

        :param msg: message to print (anything convertible to a str with '{}'.format() is OK)
        :param end: string appended after the end of the message, default a newline
        """
        self.perror(ansi.style_warning(msg), end=end, apply_style=False)

    def pexcept(self, msg: Any, *, end: str = '\n', apply_style: bool = True) -> None:
        """Print Exception message to sys.stderr. If debug is true, print exception traceback if one exists.

        :param msg: message or Exception to print
        :param end: string appended after the end of the message, default a newline
        :param apply_style: If True, then ansi.style_error will be applied to the message text. Set to False in cases
                            where the message text already has the desired style. Defaults to True.
        """
        if self.debug and sys.exc_info() != (None, None, None):
            import traceback
            traceback.print_exc()

        if isinstance(msg, Exception):
            final_msg = "EXCEPTION of type '{}' occurred with message: '{}'".format(type(msg).__name__, msg)
        else:
            final_msg = "{}".format(msg)

        if apply_style:
            final_msg = ansi.style_error(final_msg)

        if not self.debug and 'debug' in self.settable:
            warning = "\nTo enable full traceback, run the following command: 'set debug true'"
            final_msg += ansi.style_warning(warning)

        # Set apply_style to False since style has already been applied
        self.perror(final_msg, end=end, apply_style=False)

    def pfeedback(self, msg: str) -> None:
        """For printing nonessential feedback.  Can be silenced with `quiet`.
           Inclusion in redirected output is controlled by `feedback_to_output`."""
        if not self.quiet:
            if self.feedback_to_output:
                self.poutput(msg)
            else:
                ansi.ansi_aware_write(sys.stderr, "{}\n".format(msg))

    def ppaged(self, msg: str, end: str = '\n', chop: bool = False) -> None:
        """Print output using a pager if it would go off screen and stdout isn't currently being redirected.

        Never uses a pager inside of a script (Python or text) or when output is being redirected or piped or when
        stdout or stdin are not a fully functional terminal.

        :param msg: message to print to current stdout (anything convertible to a str with '{}'.format() is OK)
        :param end: string appended after the end of the message if not already present, default a newline
        :param chop: True -> causes lines longer than the screen width to be chopped (truncated) rather than wrapped
                              - truncated text is still accessible by scrolling with the right & left arrow keys
                              - chopping is ideal for displaying wide tabular data as is done in utilities like pgcli
                     False -> causes lines longer than the screen width to wrap to the next line
                              - wrapping is ideal when you want to keep users from having to use horizontal scrolling

        WARNING: On Windows, the text always wraps regardless of what the chop argument is set to
        """
        import subprocess
        if msg is not None and msg != '':
            try:
                msg_str = '{}'.format(msg)
                if not msg_str.endswith(end):
                    msg_str += end

                # Attempt to detect if we are not running within a fully functional terminal.
                # Don't try to use the pager when being run by a continuous integration system like Jenkins + pexpect.
                functional_terminal = False

                if self.stdin.isatty() and self.stdout.isatty():
                    if sys.platform.startswith('win') or os.environ.get('TERM') is not None:
                        functional_terminal = True

                # Don't attempt to use a pager that can block if redirecting or running a script (either text or Python)
                # Also only attempt to use a pager if actually running in a real fully functional terminal
                if functional_terminal and not self._redirecting and not self._in_py and not self._script_dir:
                    if ansi.allow_ansi.lower() == ansi.ANSI_NEVER.lower():
                        msg_str = ansi.strip_ansi(msg_str)

                    pager = self.pager
                    if chop:
                        pager = self.pager_chop

                    # Prevent KeyboardInterrupts while in the pager. The pager application will
                    # still receive the SIGINT since it is in the same process group as us.
                    with self.sigint_protection:
                        pipe_proc = subprocess.Popen(pager, shell=True, stdin=subprocess.PIPE)
                        pipe_proc.communicate(msg_str.encode('utf-8', 'replace'))
                else:
                    self.poutput(msg_str, end='')
            except BrokenPipeError:
                # This occurs if a command's output is being piped to another process and that process closes before the
                # command is finished. If you would like your application to print a warning message, then set the
                # broken_pipe_warning attribute to the message you want printed.`
                if self.broken_pipe_warning:
                    sys.stderr.write(self.broken_pipe_warning)

    # -----  Methods related to tab completion -----

    def _reset_completion_defaults(self) -> None:
        """
        Resets tab completion settings
        Needs to be called each time readline runs tab completion
        """
        self.allow_appended_space = True
        self.allow_closing_quote = True
        self.completion_header = ''
        self.completion_matches = []
        self.display_matches = []
        self.matches_delimited = False
        self.matches_sorted = False

        if rl_type == RlType.GNU:
            readline.set_completion_display_matches_hook(self._display_matches_gnu_readline)
        elif rl_type == RlType.PYREADLINE:
            # noinspection PyUnresolvedReferences
            readline.rl.mode._display_completions = self._display_matches_pyreadline

    def tokens_for_completion(self, line: str, begidx: int, endidx: int) -> Tuple[List[str], List[str]]:
        """Used by tab completion functions to get all tokens through the one being completed.

        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :return: A 2 item tuple where the items are
                 **On Success**
                 - tokens: list of unquoted tokens - this is generally the list needed for tab completion functions
                 - raw_tokens: list of tokens with any quotes preserved = this can be used to know if a token was quoted
                 or is missing a closing quote
                 Both lists are guaranteed to have at least 1 item. The last item in both lists is the token being tab
                 completed
                 **On Failure**
                 - Two empty lists
        """
        import copy
        unclosed_quote = ''
        quotes_to_try = copy.copy(constants.QUOTES)

        tmp_line = line[:endidx]
        tmp_endidx = endidx

        # Parse the line into tokens
        while True:
            try:
                initial_tokens = shlex_split(tmp_line[:tmp_endidx])

                # If the cursor is at an empty token outside of a quoted string,
                # then that is the token being completed. Add it to the list.
                if not unclosed_quote and begidx == tmp_endidx:
                    initial_tokens.append('')
                break
            except ValueError as ex:
                # Make sure the exception was due to an unclosed quote and
                # we haven't exhausted the closing quotes to try
                if str(ex) == "No closing quotation" and quotes_to_try:
                    # Add a closing quote and try to parse again
                    unclosed_quote = quotes_to_try[0]
                    quotes_to_try = quotes_to_try[1:]

                    tmp_line = line[:endidx]
                    tmp_line += unclosed_quote
                    tmp_endidx = endidx + 1
                else:
                    # The parsing error is not caused by unclosed quotes.
                    # Return empty lists since this means the line is malformed.
                    return [], []

        # Further split tokens on punctuation characters
        raw_tokens = self.statement_parser.split_on_punctuation(initial_tokens)

        # Save the unquoted tokens
        tokens = [utils.strip_quotes(cur_token) for cur_token in raw_tokens]

        # If the token being completed had an unclosed quote, we need
        # to remove the closing quote that was added in order for it
        # to match what was on the command line.
        if unclosed_quote:
            raw_tokens[-1] = raw_tokens[-1][:-1]

        return tokens, raw_tokens

    def delimiter_complete(self, text: str, line: str, begidx: int, endidx: int,
                           match_against: Iterable, delimiter: str) -> List[str]:
        """
        Performs tab completion against a list but each match is split on a delimiter and only
        the portion of the match being tab completed is shown as the completion suggestions.
        This is useful if you match against strings that are hierarchical in nature and have a
        common delimiter.

        An easy way to illustrate this concept is path completion since paths are just directories/files
        delimited by a slash. If you are tab completing items in /home/user you don't get the following
        as suggestions:

        /home/user/file.txt     /home/user/program.c
        /home/user/maps/        /home/user/cmd2.py

        Instead you are shown:

        file.txt                program.c
        maps/                   cmd2.py

        For a large set of data, this can be visually more pleasing and easier to search.

        Another example would be strings formatted with the following syntax: company::department::name
        In this case the delimiter would be :: and the user could easily narrow down what they are looking
        for if they were only shown suggestions in the category they are at in the string.

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param match_against: the list being matched against
        :param delimiter: what delimits each portion of the matches (ex: paths are delimited by a slash)
        :return: a list of possible tab completions
        """
        matches = utils.basic_complete(text, line, begidx, endidx, match_against)

        # Display only the portion of the match that's being completed based on delimiter
        if matches:
            # Set this to True for proper quoting of matches with spaces
            self.matches_delimited = True

            # Get the common beginning for the matches
            common_prefix = os.path.commonprefix(matches)
            prefix_tokens = common_prefix.split(delimiter)

            # Calculate what portion of the match we are completing
            display_token_index = 0
            if prefix_tokens:
                display_token_index = len(prefix_tokens) - 1

            # Get this portion for each match and store them in self.display_matches
            for cur_match in matches:
                match_tokens = cur_match.split(delimiter)
                display_token = match_tokens[display_token_index]

                if not display_token:
                    display_token = delimiter
                self.display_matches.append(display_token)

        return matches

    def flag_based_complete(self, text: str, line: str, begidx: int, endidx: int,
                            flag_dict: Dict[str, Union[Iterable, Callable]], *,
                            all_else: Union[None, Iterable, Callable] = None) -> List[str]:
        """Tab completes based on a particular flag preceding the token being completed.

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param flag_dict: dictionary whose structure is the following:
                          `keys` - flags (ex: -c, --create) that result in tab completion for the next argument in the
                          command line
                          `values` - there are two types of values:
                          1. iterable list of strings to match against (dictionaries, lists, etc.)
                          2. function that performs tab completion (ex: path_complete)
        :param all_else: an optional parameter for tab completing any token that isn't preceded by a flag in flag_dict
        :return: a list of possible tab completions
        """
        # Get all tokens through the one being completed
        tokens, _ = self.tokens_for_completion(line, begidx, endidx)
        if not tokens:
            return []

        completions_matches = []
        match_against = all_else

        # Must have at least 2 args for a flag to precede the token being completed
        if len(tokens) > 1:
            flag = tokens[-2]
            if flag in flag_dict:
                match_against = flag_dict[flag]

        # Perform tab completion using an Iterable
        if isinstance(match_against, Iterable):
            completions_matches = utils.basic_complete(text, line, begidx, endidx, match_against)

        # Perform tab completion using a function
        elif callable(match_against):
            completions_matches = match_against(text, line, begidx, endidx)

        return completions_matches

    def index_based_complete(self, text: str, line: str, begidx: int, endidx: int,
                             index_dict: Mapping[int, Union[Iterable, Callable]], *,
                             all_else: Union[None, Iterable, Callable] = None) -> List[str]:
        """Tab completes based on a fixed position in the input string.

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param index_dict: dictionary whose structure is the following:
                           `keys` - 0-based token indexes into command line that determine which tokens perform tab
                           completion
                           `values` - there are two types of values:
                           1. iterable list of strings to match against (dictionaries, lists, etc.)
                           2. function that performs tab completion (ex: path_complete)
        :param all_else: an optional parameter for tab completing any token that isn't at an index in index_dict
        :return: a list of possible tab completions
        """
        # Get all tokens through the one being completed
        tokens, _ = self.tokens_for_completion(line, begidx, endidx)
        if not tokens:
            return []

        matches = []

        # Get the index of the token being completed
        index = len(tokens) - 1

        # Check if token is at an index in the dictionary
        if index in index_dict:
            match_against = index_dict[index]
        else:
            match_against = all_else

        # Perform tab completion using a Iterable
        if isinstance(match_against, Iterable):
            matches = utils.basic_complete(text, line, begidx, endidx, match_against)

        # Perform tab completion using a function
        elif callable(match_against):
            matches = match_against(text, line, begidx, endidx)

        return matches

    # noinspection PyUnusedLocal
    def path_complete(self, text: str, line: str, begidx: int, endidx: int, *,
                      path_filter: Optional[Callable[[str], bool]] = None) -> List[str]:
        """Performs completion of local file system paths

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param path_filter: optional filter function that determines if a path belongs in the results
                            this function takes a path as its argument and returns True if the path should
                            be kept in the results
        :return: a list of possible tab completions
        """

        # Used to complete ~ and ~user strings
        def complete_users() -> List[str]:

            # We are returning ~user strings that resolve to directories,
            # so don't append a space or quote in the case of a single result.
            self.allow_appended_space = False
            self.allow_closing_quote = False

            users = []

            # Windows lacks the pwd module so we can't get a list of users.
            # Instead we will return a result once the user enters text that
            # resolves to an existing home directory.
            if sys.platform.startswith('win'):
                expanded_path = os.path.expanduser(text)
                if os.path.isdir(expanded_path):
                    user = text
                    if add_trailing_sep_if_dir:
                        user += os.path.sep
                    users.append(user)
            else:
                import pwd

                # Iterate through a list of users from the password database
                for cur_pw in pwd.getpwall():

                    # Check if the user has an existing home dir
                    if os.path.isdir(cur_pw.pw_dir):

                        # Add a ~ to the user to match against text
                        cur_user = '~' + cur_pw.pw_name
                        if cur_user.startswith(text):
                            if add_trailing_sep_if_dir:
                                cur_user += os.path.sep
                            users.append(cur_user)

            return users

        # Determine if a trailing separator should be appended to directory completions
        add_trailing_sep_if_dir = False
        if endidx == len(line) or (endidx < len(line) and line[endidx] != os.path.sep):
            add_trailing_sep_if_dir = True

        # Used to replace cwd in the final results
        cwd = os.getcwd()
        cwd_added = False

        # Used to replace expanded user path in final result
        orig_tilde_path = ''
        expanded_tilde_path = ''

        # If the search text is blank, then search in the CWD for *
        if not text:
            search_str = os.path.join(os.getcwd(), '*')
            cwd_added = True
        else:
            # Purposely don't match any path containing wildcards
            wildcards = ['*', '?']
            for wildcard in wildcards:
                if wildcard in text:
                    return []

            # Start the search string
            search_str = text + '*'

            # Handle tilde expansion and completion
            if text.startswith('~'):
                sep_index = text.find(os.path.sep, 1)

                # If there is no slash, then the user is still completing the user after the tilde
                if sep_index == -1:
                    return complete_users()

                # Otherwise expand the user dir
                else:
                    search_str = os.path.expanduser(search_str)

                    # Get what we need to restore the original tilde path later
                    orig_tilde_path = text[:sep_index]
                    expanded_tilde_path = os.path.expanduser(orig_tilde_path)

            # If the search text does not have a directory, then use the cwd
            elif not os.path.dirname(text):
                search_str = os.path.join(os.getcwd(), search_str)
                cwd_added = True

        # Set this to True for proper quoting of paths with spaces
        self.matches_delimited = True

        # Find all matching path completions
        matches = glob.glob(search_str)

        # Filter out results that don't belong
        if path_filter is not None:
            matches = [c for c in matches if path_filter(c)]

        # Don't append a space or closing quote to directory
        if len(matches) == 1 and os.path.isdir(matches[0]):
            self.allow_appended_space = False
            self.allow_closing_quote = False

        # Sort the matches before any trailing slashes are added
        matches.sort(key=self.default_sort_key)
        self.matches_sorted = True

        # Build display_matches and add a slash to directories
        for index, cur_match in enumerate(matches):

            # Display only the basename of this path in the tab-completion suggestions
            self.display_matches.append(os.path.basename(cur_match))

            # Add a separator after directories if the next character isn't already a separator
            if os.path.isdir(cur_match) and add_trailing_sep_if_dir:
                matches[index] += os.path.sep
                self.display_matches[index] += os.path.sep

        # Remove cwd if it was added to match the text readline expects
        if cwd_added:
            if cwd == os.path.sep:
                to_replace = cwd
            else:
                to_replace = cwd + os.path.sep
            matches = [cur_path.replace(to_replace, '', 1) for cur_path in matches]

        # Restore the tilde string if we expanded one to match the text readline expects
        if expanded_tilde_path:
            matches = [cur_path.replace(expanded_tilde_path, orig_tilde_path, 1) for cur_path in matches]

        return matches

    def shell_cmd_complete(self, text: str, line: str, begidx: int, endidx: int, *,
                           complete_blank: bool = False) -> List[str]:
        """Performs completion of executables either in a user's path or a given path

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param complete_blank: If True, then a blank will complete all shell commands in a user's path. If False, then
                               no completion is performed. Defaults to False to match Bash shell behavior.
        :return: a list of possible tab completions
        """
        # Don't tab complete anything if no shell command has been started
        if not complete_blank and not text:
            return []

        # If there are no path characters in the search text, then do shell command completion in the user's path
        if not text.startswith('~') and os.path.sep not in text:
            return utils.get_exes_in_path(text)

        # Otherwise look for executables in the given path
        else:
            return self.path_complete(text, line, begidx, endidx,
                                      path_filter=lambda path: os.path.isdir(path) or os.access(path, os.X_OK))

    def _redirect_complete(self, text: str, line: str, begidx: int, endidx: int, compfunc: Callable) -> List[str]:
        """Called by complete() as the first tab completion function for all commands
        It determines if it should tab complete for redirection (|, >, >>) or use the
        completer function for the current command

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param compfunc: the completer function for the current command
                         this will be called if we aren't completing for redirection
        :return: a list of possible tab completions
        """
        # Get all tokens through the one being completed. We want the raw tokens
        # so we can tell if redirection strings are quoted and ignore them.
        _, raw_tokens = self.tokens_for_completion(line, begidx, endidx)
        if not raw_tokens:
            return []

        # Must at least have the command
        if len(raw_tokens) > 1:

            # True when command line contains any redirection tokens
            has_redirection = False

            # Keep track of state while examining tokens
            in_pipe = False
            in_file_redir = False
            do_shell_completion = False
            do_path_completion = False
            prior_token = None

            for cur_token in raw_tokens:
                # Process redirection tokens
                if cur_token in constants.REDIRECTION_TOKENS:
                    has_redirection = True

                    # Check if we are at a pipe
                    if cur_token == constants.REDIRECTION_PIPE:
                        # Do not complete bad syntax (e.g cmd | |)
                        if prior_token == constants.REDIRECTION_PIPE:
                            return []

                        in_pipe = True
                        in_file_redir = False

                    # Otherwise this is a file redirection token
                    else:
                        if prior_token in constants.REDIRECTION_TOKENS or in_file_redir:
                            # Do not complete bad syntax (e.g cmd | >) (e.g cmd > blah >)
                            return []

                        in_pipe = False
                        in_file_redir = True

                # Not a redirection token
                else:
                    do_shell_completion = False
                    do_path_completion = False

                    if prior_token == constants.REDIRECTION_PIPE:
                        do_shell_completion = True
                    elif in_pipe or prior_token in (constants.REDIRECTION_OUTPUT, constants.REDIRECTION_APPEND):
                        do_path_completion = True

                prior_token = cur_token

            if do_shell_completion:
                return self.shell_cmd_complete(text, line, begidx, endidx)

            elif do_path_completion:
                return self.path_complete(text, line, begidx, endidx)

            # If there were redirection strings anywhere on the command line, then we
            # are no longer tab completing for the current command
            elif has_redirection:
                return []

        # Call the command's completer function
        return compfunc(text, line, begidx, endidx)

    @staticmethod
    def _pad_matches_to_display(matches_to_display: List[str]) -> Tuple[List[str], int]:  # pragma: no cover
        """Adds padding to the matches being displayed as tab completion suggestions.
        The default padding of readline/pyreadine is small and not visually appealing
        especially if matches have spaces. It appears very squished together.

        :param matches_to_display: the matches being padded
        :return: the padded matches and length of padding that was added
        """
        if rl_type == RlType.GNU:
            # Add 2 to the padding of 2 that readline uses for a total of 4.
            padding = 2 * ' '

        elif rl_type == RlType.PYREADLINE:
            # Add 3 to the padding of 1 that pyreadline uses for a total of 4.
            padding = 3 * ' '

        else:
            return matches_to_display, 0

        return [cur_match + padding for cur_match in matches_to_display], len(padding)

    def _display_matches_gnu_readline(self, substitution: str, matches: List[str],
                                      longest_match_length: int) -> None:  # pragma: no cover
        """Prints a match list using GNU readline's rl_display_match_list()
        This exists to print self.display_matches if it has data. Otherwise matches prints.

        :param substitution: the substitution written to the command line
        :param matches: the tab completion matches to display
        :param longest_match_length: longest printed length of the matches
        """
        if rl_type == RlType.GNU:

            # Check if we should show display_matches
            if self.display_matches:
                matches_to_display = self.display_matches

                # Recalculate longest_match_length for display_matches
                longest_match_length = 0

                for cur_match in matches_to_display:
                    cur_length = ansi.ansi_safe_wcswidth(cur_match)
                    if cur_length > longest_match_length:
                        longest_match_length = cur_length
            else:
                matches_to_display = matches

            # Add padding for visual appeal
            matches_to_display, padding_length = self._pad_matches_to_display(matches_to_display)
            longest_match_length += padding_length

            # We will use readline's display function (rl_display_match_list()), so we
            # need to encode our string as bytes to place in a C array.
            encoded_substitution = bytes(substitution, encoding='utf-8')
            encoded_matches = [bytes(cur_match, encoding='utf-8') for cur_match in matches_to_display]

            # rl_display_match_list() expects matches to be in argv format where
            # substitution is the first element, followed by the matches, and then a NULL.
            # noinspection PyCallingNonCallable,PyTypeChecker
            strings_array = (ctypes.c_char_p * (1 + len(encoded_matches) + 1))()

            # Copy in the encoded strings and add a NULL to the end
            strings_array[0] = encoded_substitution
            strings_array[1:-1] = encoded_matches
            strings_array[-1] = None

            # Print the header if one exists
            if self.completion_header:
                sys.stdout.write('\n' + self.completion_header)

            # Call readline's display function
            # rl_display_match_list(strings_array, number of completion matches, longest match length)
            readline_lib.rl_display_match_list(strings_array, len(encoded_matches), longest_match_length)

            # Redraw prompt and input line
            rl_force_redisplay()

    def _display_matches_pyreadline(self, matches: List[str]) -> None:  # pragma: no cover
        """Prints a match list using pyreadline's _display_completions()
        This exists to print self.display_matches if it has data. Otherwise matches prints.

        :param matches: the tab completion matches to display
        """
        if rl_type == RlType.PYREADLINE:

            # Check if we should show display_matches
            if self.display_matches:
                matches_to_display = self.display_matches
            else:
                matches_to_display = matches

            # Add padding for visual appeal
            matches_to_display, _ = self._pad_matches_to_display(matches_to_display)

            # Print the header if one exists
            if self.completion_header:
                # noinspection PyUnresolvedReferences
                readline.rl.mode.console.write('\n' + self.completion_header)

            # Display matches using actual display function. This also redraws the prompt and line.
            orig_pyreadline_display(matches_to_display)

    def _completion_for_command(self, text: str, line: str, begidx: int,
                                endidx: int, shortcut_to_restore: str) -> None:
        """
        Helper function for complete() that performs command-specific tab completion

        :param text: the string prefix we are attempting to match (all matches must begin with it)
        :param line: the current input line with leading whitespace removed
        :param begidx: the beginning index of the prefix text
        :param endidx: the ending index of the prefix text
        :param shortcut_to_restore: if not blank, then this shortcut was removed from text and needs to be
                                    prepended to all the matches
        """
        unclosed_quote = ''

        # Parse the command line
        statement = self.statement_parser.parse_command_only(line)
        command = statement.command
        expanded_line = statement.command_and_args

        # We overwrote line with a properly formatted but fully stripped version
        # Restore the end spaces since line is only supposed to be lstripped when
        # passed to completer functions according to Python docs
        rstripped_len = len(line) - len(line.rstrip())
        expanded_line += ' ' * rstripped_len

        # Fix the index values if expanded_line has a different size than line
        if len(expanded_line) != len(line):
            diff = len(expanded_line) - len(line)
            begidx += diff
            endidx += diff

        # Overwrite line to pass into completers
        line = expanded_line

        # Get all tokens through the one being completed
        tokens, raw_tokens = self.tokens_for_completion(line, begidx, endidx)

        # Check if we either had a parsing error or are trying to complete the command token
        # The latter can happen if " or ' was entered as the command
        if len(tokens) <= 1:
            return

        # Text we need to remove from completions later
        text_to_remove = ''

        # Get the token being completed with any opening quote preserved
        raw_completion_token = raw_tokens[-1]

        # Check if the token being completed has an opening quote
        if raw_completion_token and raw_completion_token[0] in constants.QUOTES:

            # Since the token is still being completed, we know the opening quote is unclosed
            unclosed_quote = raw_completion_token[0]

            # readline still performs word breaks after a quote. Therefore something like quoted search
            # text with a space would have resulted in begidx pointing to the middle of the token we
            # we want to complete. Figure out where that token actually begins and save the beginning
            # portion of it that was not part of the text readline gave us. We will remove it from the
            # completions later since readline expects them to start with the original text.
            actual_begidx = line[:endidx].rfind(tokens[-1])

            if actual_begidx != begidx:
                text_to_remove = line[actual_begidx:begidx]

                # Adjust text and where it begins so the completer routines
                # get unbroken search text to complete on.
                text = text_to_remove + text
                begidx = actual_begidx

        # Check if a macro was entered
        if command in self.macros:
            compfunc = self.path_complete

        # Check if a command was entered
        elif command in self.get_all_commands():
            # Get the completer function for this command
            compfunc = getattr(self, COMPLETER_FUNC_PREFIX + command, None)

            if compfunc is None:
                # There's no completer function, next see if the command uses argparse
                func = self.cmd_func(command)
                argparser = getattr(func, CMD_ATTR_ARGPARSER, None)

                if func is not None and argparser is not None:
                    import functools
                    compfunc = functools.partial(self._autocomplete_default,
                                                 argparser=argparser)
                else:
                    compfunc = self.completedefault

        # Not a recognized macro or command
        else:
            # Check if this command should be run as a shell command
            if self.default_to_shell and command in utils.get_exes_in_path(command):
                compfunc = self.path_complete
            else:
                compfunc = self.completedefault

        # Attempt tab completion for redirection first, and if that isn't occurring,
        # call the completer function for the current command
        self.completion_matches = self._redirect_complete(text, line, begidx, endidx, compfunc)

        if self.completion_matches:

            # Eliminate duplicates
            self.completion_matches = utils.remove_duplicates(self.completion_matches)
            self.display_matches = utils.remove_duplicates(self.display_matches)

            if not self.display_matches:
                # Since self.display_matches is empty, set it to self.completion_matches
                # before we alter them. That way the suggestions will reflect how we parsed
                # the token being completed and not how readline did.
                import copy
                self.display_matches = copy.copy(self.completion_matches)

            # Check if we need to add an opening quote
            if not unclosed_quote:

                add_quote = False

                # This is the tab completion text that will appear on the command line.
                common_prefix = os.path.commonprefix(self.completion_matches)

                if self.matches_delimited:
                    # Check if any portion of the display matches appears in the tab completion
                    display_prefix = os.path.commonprefix(self.display_matches)

                    # For delimited matches, we check for a space in what appears before the display
                    # matches (common_prefix) as well as in the display matches themselves.
                    if ' ' in common_prefix or (display_prefix
                                                and any(' ' in match for match in self.display_matches)):
                        add_quote = True

                # If there is a tab completion and any match has a space, then add an opening quote
                elif common_prefix and any(' ' in match for match in self.completion_matches):
                    add_quote = True

                if add_quote:
                    # Figure out what kind of quote to add and save it as the unclosed_quote
                    if any('"' in match for match in self.completion_matches):
                        unclosed_quote = "'"
                    else:
                        unclosed_quote = '"'

                    self.completion_matches = [unclosed_quote + match for match in self.completion_matches]

            # Check if we need to remove text from the beginning of tab completions
            elif text_to_remove:
                self.completion_matches = [match.replace(text_to_remove, '', 1) for match in self.completion_matches]

            # Check if we need to restore a shortcut in the tab completions
            # so it doesn't get erased from the command line
            if shortcut_to_restore:
                self.completion_matches = [shortcut_to_restore + match for match in self.completion_matches]

            # If we have one result, then add a closing quote if needed and allowed
            if len(self.completion_matches) == 1 and self.allow_closing_quote and unclosed_quote:
                self.completion_matches[0] += unclosed_quote

    def complete(self, text: str, state: int) -> Optional[str]:
        """Override of cmd2's complete method which returns the next possible completion for 'text'

        This completer function is called by readline as complete(text, state), for state in 0, 1, 2, …,
        until it returns a non-string value. It should return the next possible completion starting with text.

        Since readline suppresses any exception raised in completer functions, they can be difficult to debug.
        Therefore this function wraps the actual tab completion logic and prints to stderr any exception that
        occurs before returning control to readline.

        :param text: the current word that user is typing
        :param state: non-negative integer
        :return: the next possible completion for text or None
        """
        # noinspection PyBroadException
        try:
            if state == 0 and rl_type != RlType.NONE:
                self._reset_completion_defaults()

                # Check if we are completing a multiline command
                if self._at_continuation_prompt:
                    # lstrip and prepend the previously typed portion of this multiline command
                    lstripped_previous = self._multiline_in_progress.lstrip()
                    line = lstripped_previous + readline.get_line_buffer()

                    # Increment the indexes to account for the prepended text
                    begidx = len(lstripped_previous) + readline.get_begidx()
                    endidx = len(lstripped_previous) + readline.get_endidx()
                else:
                    # lstrip the original line
                    orig_line = readline.get_line_buffer()
                    line = orig_line.lstrip()
                    num_stripped = len(orig_line) - len(line)

                    # Calculate new indexes for the stripped line. If the cursor is at a position before the end of a
                    # line of spaces, then the following math could result in negative indexes. Enforce a max of 0.
                    begidx = max(readline.get_begidx() - num_stripped, 0)
                    endidx = max(readline.get_endidx() - num_stripped, 0)

                # Shortcuts are not word break characters when tab completing. Therefore shortcuts become part
                # of the text variable if there isn't a word break, like a space, after it. We need to remove it
                # from text and update the indexes. This only applies if we are at the the beginning of the line.
                shortcut_to_restore = ''
                if begidx == 0:
                    for (shortcut, _) in self.statement_parser.shortcuts:
                        if text.startswith(shortcut):
                            # Save the shortcut to restore later
                            shortcut_to_restore = shortcut

                            # Adjust text and where it begins
                            text = text[len(shortcut_to_restore):]
                            begidx += len(shortcut_to_restore)
                            break

                # If begidx is greater than 0, then we are no longer completing the first token (command name)
                if begidx > 0:
                    self._completion_for_command(text, line, begidx, endidx, shortcut_to_restore)

                # Otherwise complete token against anything a user can run
                else:
                    match_against = self._get_commands_aliases_and_macros_for_completion()
                    self.completion_matches = utils.basic_complete(text, line, begidx, endidx, match_against)

                # If we have one result and we are at the end of the line, then add a space if allowed
                if len(self.completion_matches) == 1 and endidx == len(line) and self.allow_appended_space:
                    self.completion_matches[0] += ' '

                # Sort matches if they haven't already been sorted
                if not self.matches_sorted:
                    self.completion_matches.sort(key=self.default_sort_key)
                    self.display_matches.sort(key=self.default_sort_key)
                    self.matches_sorted = True

            try:
                return self.completion_matches[state]
            except IndexError:
                return None

        except Exception as e:
            # Insert a newline so the exception doesn't print in the middle of the command line being tab completed
            self.perror('\n', end='')
            self.pexcept(e)
            return None

    def _autocomplete_default(self, text: str, line: str, begidx: int, endidx: int,
                              argparser: argparse.ArgumentParser) -> List[str]:
        """Default completion function for argparse commands"""
        from .argparse_completer import AutoCompleter
        completer = AutoCompleter(argparser, self)
        tokens, _ = self.tokens_for_completion(line, begidx, endidx)
        return completer.complete_command(tokens, text, line, begidx, endidx)

    def get_all_commands(self) -> List[str]:
        """Return a list of all commands"""
        return [name[len(COMMAND_FUNC_PREFIX):] for name in self.get_names()
                if name.startswith(COMMAND_FUNC_PREFIX) and callable(getattr(self, name))]

    def get_visible_commands(self) -> List[str]:
        """Return a list of commands that have not been hidden or disabled"""
        commands = self.get_all_commands()

        # Remove the hidden commands
        for name in self.hidden_commands:
            if name in commands:
                commands.remove(name)

        # Remove the disabled commands
        for name in self.disabled_commands:
            if name in commands:
                commands.remove(name)

        return commands

    def _get_alias_completion_items(self) -> List[CompletionItem]:
        """Return list of current alias names and values as CompletionItems"""
        return [CompletionItem(cur_key, self.aliases[cur_key]) for cur_key in self.aliases]

    def _get_macro_completion_items(self) -> List[CompletionItem]:
        """Return list of current macro names and values as CompletionItems"""
        return [CompletionItem(cur_key, self.macros[cur_key].value) for cur_key in self.macros]

    def _get_settable_completion_items(self) -> List[CompletionItem]:
        """Return list of current settable names and descriptions as CompletionItems"""
        return [CompletionItem(cur_key, self.settable[cur_key]) for cur_key in self.settable]

    def _get_commands_aliases_and_macros_for_completion(self) -> List[str]:
        """Return a list of visible commands, aliases, and macros for tab completion"""
        visible_commands = set(self.get_visible_commands())
        alias_names = set(self.aliases)
        macro_names = set(self.macros)
        return list(visible_commands | alias_names | macro_names)

    def get_help_topics(self) -> List[str]:
        """ Returns a list of help topics """
        return [name[len(HELP_FUNC_PREFIX):] for name in self.get_names()
                if name.startswith(HELP_FUNC_PREFIX) and callable(getattr(self, name))]

    # noinspection PyUnusedLocal
    def sigint_handler(self, signum: int, frame) -> None:
        """Signal handler for SIGINTs which typically come from Ctrl-C events.

        If you need custom SIGINT behavior, then override this function.

        :param signum: signal number
        :param frame: required param for signal handlers
        """
        if self._cur_pipe_proc_reader is not None:
            # Pass the SIGINT to the current pipe process
            self._cur_pipe_proc_reader.send_sigint()

        # Check if we are allowed to re-raise the KeyboardInterrupt
        if not self.sigint_protection:
            raise KeyboardInterrupt("Got a keyboard interrupt")

    def precmd(self, statement: Statement) -> Statement:
        """Hook method executed just before the command is processed by ``onecmd()`` and after adding it to the history.

        :param statement: subclass of str which also contains the parsed input
        :return: a potentially modified version of the input Statement object
        """
        return statement

    def parseline(self, line: str) -> Tuple[str, str, str]:
        """Parse the line into a command name and a string containing the arguments.

        NOTE: This is an override of a parent class method.  It is only used by other parent class methods.

        Different from the parent class method, this ignores self.identchars.

        :param line: line read by readline
        :return: tuple containing (command, args, line)
        """
        statement = self.statement_parser.parse_command_only(line)
        return statement.command, statement.args, statement.command_and_args

    def onecmd_plus_hooks(self, line: str, *, add_to_history: bool = True, py_bridge_call: bool = False) -> bool:
        """Top-level function called by cmdloop() to handle parsing a line and running the command and all of its hooks.

        :param line: command line to run
        :param add_to_history: If True, then add this command to history. Defaults to True.
        :param py_bridge_call: This should only ever be set to True by PyBridge to signify the beginning
                               of an app() call from Python. It is used to enable/disable the storage of the
                               command's stdout.
        :return: True if running of commands should stop
        """
        import datetime

        stop = False
        try:
            statement = self._input_line_to_statement(line)
        except EmptyStatement:
            return self._run_cmdfinalization_hooks(stop, None)
        except ValueError as ex:
            # If shlex.split failed on syntax, let user know what's going on
            self.pexcept("Invalid syntax: {}".format(ex))
            return stop

        # now that we have a statement, run it with all the hooks
        try:
            # call the postparsing hooks
            data = plugin.PostparsingData(False, statement)
            for func in self._postparsing_hooks:
                data = func(data)
                if data.stop:
                    break
            # unpack the data object
            statement = data.statement
            stop = data.stop
            if stop:
                # we should not run the command, but
                # we need to run the finalization hooks
                raise EmptyStatement

            # Keep track of whether or not we were already _redirecting before this command
            already_redirecting = self._redirecting

            # This will be a utils.RedirectionSavedState object for the command
            saved_state = None

            try:
                # Get sigint protection while we set up redirection
                with self.sigint_protection:
                    if py_bridge_call:
                        # Start saving command's stdout at this point
                        self.stdout.pause_storage = False

                    redir_error, saved_state = self._redirect_output(statement)
                    self._cur_pipe_proc_reader = saved_state.pipe_proc_reader

                # Do not continue if an error occurred while trying to redirect
                if not redir_error:
                    # See if we need to update self._redirecting
                    if not already_redirecting:
                        self._redirecting = saved_state.redirecting

                    timestart = datetime.datetime.now()

                    # precommand hooks
                    data = plugin.PrecommandData(statement)
                    for func in self._precmd_hooks:
                        data = func(data)
                    statement = data.statement

                    # call precmd() for compatibility with cmd.Cmd
                    statement = self.precmd(statement)

                    # go run the command function
                    stop = self.onecmd(statement, add_to_history=add_to_history)

                    # postcommand hooks
                    data = plugin.PostcommandData(stop, statement)
                    for func in self._postcmd_hooks:
                        data = func(data)

                    # retrieve the final value of stop, ignoring any statement modification from the hooks
                    stop = data.stop

                    # call postcmd() for compatibility with cmd.Cmd
                    stop = self.postcmd(stop, statement)

                    if self.timing:
                        self.pfeedback('Elapsed: {}'.format(datetime.datetime.now() - timestart))
            finally:
                # Get sigint protection while we restore stuff
                with self.sigint_protection:
                    if saved_state is not None:
                        self._restore_output(statement, saved_state)

                    if not already_redirecting:
                        self._redirecting = False

                    if py_bridge_call:
                        # Stop saving command's stdout before command finalization hooks run
                        self.stdout.pause_storage = True

        except EmptyStatement:
            # don't do anything, but do allow command finalization hooks to run
            pass
        except Exception as ex:
            self.pexcept(ex)
        finally:
            return self._run_cmdfinalization_hooks(stop, statement)

    def _run_cmdfinalization_hooks(self, stop: bool, statement: Optional[Statement]) -> bool:
        """Run the command finalization hooks"""

        with self.sigint_protection:
            if not sys.platform.startswith('win') and self.stdout.isatty():
                # Before the next command runs, fix any terminal problems like those
                # caused by certain binary characters having been printed to it.
                import subprocess
                proc = subprocess.Popen(['stty', 'sane'])
                proc.communicate()

        try:
            data = plugin.CommandFinalizationData(stop, statement)
            for func in self._cmdfinalization_hooks:
                data = func(data)
            # retrieve the final value of stop, ignoring any
            # modifications to the statement
            return data.stop
        except Exception as ex:
            self.pexcept(ex)

    def runcmds_plus_hooks(self, cmds: List[Union[HistoryItem, str]], *, add_to_history: bool = True) -> bool:
        """
        Used when commands are being run in an automated fashion like text scripts or history replays.
        The prompt and command line for each command will be printed if echo is True.

        :param cmds: commands to run
        :param add_to_history: If True, then add these commands to history. Defaults to True.
        :return: True if running of commands should stop
        """
        for line in cmds:
            if isinstance(line, HistoryItem):
                line = line.raw

            if self.echo:
                self.poutput('{}{}'.format(self.prompt, line))

            if self.onecmd_plus_hooks(line, add_to_history=add_to_history):
                return True

        return False

    def _complete_statement(self, line: str) -> Statement:
        """Keep accepting lines of input until the command is complete.

        There is some pretty hacky code here to handle some quirks of
        self.pseudo_raw_input(). It returns a literal 'eof' if the input
        pipe runs out. We can't refactor it because we need to retain
        backwards compatibility with the standard library version of cmd.

        :param line: the line being parsed
        :return: the completed Statement
        """
        while True:
            try:
                statement = self.statement_parser.parse(line)
                if statement.multiline_command and statement.terminator:
                    # we have a completed multiline command, we are done
                    break
                if not statement.multiline_command:
                    # it's not a multiline command, but we parsed it ok
                    # so we are done
                    break
            except ValueError:
                # we have unclosed quotation marks, lets parse only the command
                # and see if it's a multiline
                statement = self.statement_parser.parse_command_only(line)
                if not statement.multiline_command:
                    # not a multiline command, so raise the exception
                    raise

            # if we get here we must have:
            #   - a multiline command with no terminator
            #   - a multiline command with unclosed quotation marks
            try:
                self._at_continuation_prompt = True

                # Save the command line up to this point for tab completion
                self._multiline_in_progress = line + '\n'

                nextline = self._pseudo_raw_input(self.continuation_prompt)
                if nextline == 'eof':
                    # they entered either a blank line, or we hit an EOF
                    # for some other reason. Turn the literal 'eof'
                    # into a blank line, which serves as a command
                    # terminator
                    nextline = '\n'
                    self.poutput(nextline)
                line = '{}{}'.format(self._multiline_in_progress, nextline)
            except KeyboardInterrupt as ex:
                if self.quit_on_sigint:
                    raise ex
                else:
                    self.poutput('^C')
                    statement = self.statement_parser.parse('')
                    break
            finally:
                self._at_continuation_prompt = False

        if not statement.command:
            raise EmptyStatement()
        return statement

    def _input_line_to_statement(self, line: str) -> Statement:
        """
        Parse the user's input line and convert it to a Statement, ensuring that all macros are also resolved

        :param line: the line being parsed
        :return: parsed command line as a Statement
        """
        used_macros = []
        orig_line = None

        # Continue until all macros are resolved
        while True:
            # Make sure all input has been read and convert it to a Statement
            statement = self._complete_statement(line)

            # Save the fully entered line if this is the first loop iteration
            if orig_line is None:
                orig_line = statement.raw

            # Check if this command matches a macro and wasn't already processed to avoid an infinite loop
            if statement.command in self.macros.keys() and statement.command not in used_macros:
                used_macros.append(statement.command)
                line = self._resolve_macro(statement)
                if line is None:
                    raise EmptyStatement()
            else:
                break

        # This will be true when a macro was used
        if orig_line != statement.raw:
            # Build a Statement that contains the resolved macro line
            # but the originally typed line for its raw member.
            statement = Statement(statement.args,
                                  raw=orig_line,
                                  command=statement.command,
                                  arg_list=statement.arg_list,
                                  multiline_command=statement.multiline_command,
                                  terminator=statement.terminator,
                                  suffix=statement.suffix,
                                  pipe_to=statement.pipe_to,
                                  output=statement.output,
                                  output_to=statement.output_to)
        return statement

    def _resolve_macro(self, statement: Statement) -> Optional[str]:
        """
        Resolve a macro and return the resulting string

        :param statement: the parsed statement from the command line
        :return: the resolved macro or None on error
        """
        if statement.command not in self.macros.keys():
            raise KeyError('{} is not a macro'.format(statement.command))

        macro = self.macros[statement.command]

        # Make sure enough arguments were passed in
        if len(statement.arg_list) < macro.minimum_arg_count:
            self.perror(
                "The macro '{}' expects at least {} argument(s)".format(
                    statement.command,
                    macro.minimum_arg_count
                )
            )
            return None

        # Resolve the arguments in reverse and read their values from statement.argv since those
        # are unquoted. Macro args should have been quoted when the macro was created.
        resolved = macro.value
        reverse_arg_list = sorted(macro.arg_list, key=lambda ma: ma.start_index, reverse=True)

        for arg in reverse_arg_list:
            if arg.is_escaped:
                to_replace = '{{' + arg.number_str + '}}'
                replacement = '{' + arg.number_str + '}'
            else:
                to_replace = '{' + arg.number_str + '}'
                replacement = statement.argv[int(arg.number_str)]

            parts = resolved.rsplit(to_replace, maxsplit=1)
            resolved = parts[0] + replacement + parts[1]

        # Append extra arguments and use statement.arg_list since these arguments need their quotes preserved
        for arg in statement.arg_list[macro.minimum_arg_count:]:
            resolved += ' ' + arg

        # Restore any terminator, suffix, redirection, etc.
        return resolved + statement.post_command

    def _redirect_output(self, statement: Statement) -> Tuple[bool, utils.RedirectionSavedState]:
        """Handles output redirection for >, >>, and |.

        :param statement: a parsed statement from the user
        :return: A bool telling if an error occurred and a utils.RedirectionSavedState object
        """
        import io
        import subprocess

        redir_error = False

        # Initialize the saved state
        saved_state = utils.RedirectionSavedState(self.stdout, sys.stdout, self._cur_pipe_proc_reader)

        if not self.allow_redirection:
            return redir_error, saved_state

        if statement.pipe_to:
            # Create a pipe with read and write sides
            read_fd, write_fd = os.pipe()

            # Open each side of the pipe
            subproc_stdin = io.open(read_fd, 'r')
            new_stdout = io.open(write_fd, 'w')

            # Set options to not forward signals to the pipe process. If a Ctrl-C event occurs,
            # our sigint handler will forward it only to the most recent pipe process. This makes
            # sure pipe processes close in the right order (most recent first).
            if sys.platform == 'win32':
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
                start_new_session = False
            else:
                creationflags = 0
                start_new_session = True

            # For any stream that is a StdSim, we will use a pipe so we can capture its output
            proc = subprocess.Popen(statement.pipe_to,
                                    stdin=subproc_stdin,
                                    stdout=subprocess.PIPE if isinstance(self.stdout, utils.StdSim) else self.stdout,
                                    stderr=subprocess.PIPE if isinstance(sys.stderr, utils.StdSim) else sys.stderr,
                                    creationflags=creationflags,
                                    start_new_session=start_new_session,
                                    shell=True)

            # Popen was called with shell=True so the user can chain pipe commands and redirect their output
            # like: !ls -l | grep user | wc -l > out.txt. But this makes it difficult to know if the pipe process
            # started OK, since the shell itself always starts. Therefore, we will wait a short time and check
            # if the pipe process is still running.
            try:
                proc.wait(0.2)
            except subprocess.TimeoutExpired:
                pass

            # Check if the pipe process already exited
            if proc.returncode is not None:
                self.perror('Pipe process exited with code {} before command could run'.format(proc.returncode))
                subproc_stdin.close()
                new_stdout.close()
                redir_error = True
            else:
                saved_state.redirecting = True
                saved_state.pipe_proc_reader = utils.ProcReader(proc, self.stdout, sys.stderr)
                sys.stdout = self.stdout = new_stdout

        elif statement.output:
            import tempfile
            if (not statement.output_to) and (not self._can_clip):
                self.perror("Cannot redirect to paste buffer; install 'pyperclip' and re-run to enable")
                redir_error = True

            elif statement.output_to:
                # going to a file
                mode = 'w'
                # statement.output can only contain
                # REDIRECTION_APPEND or REDIRECTION_OUTPUT
                if statement.output == constants.REDIRECTION_APPEND:
                    mode = 'a'
                try:
                    new_stdout = open(utils.strip_quotes(statement.output_to), mode)
                    saved_state.redirecting = True
                    sys.stdout = self.stdout = new_stdout
                except OSError as ex:
                    self.pexcept('Failed to redirect because - {}'.format(ex))
                    redir_error = True
            else:
                # going to a paste buffer
                new_stdout = tempfile.TemporaryFile(mode="w+")
                saved_state.redirecting = True
                sys.stdout = self.stdout = new_stdout

                if statement.output == constants.REDIRECTION_APPEND:
                    self.stdout.write(get_paste_buffer())
                    self.stdout.flush()

        return redir_error, saved_state

    def _restore_output(self, statement: Statement, saved_state: utils.RedirectionSavedState) -> None:
        """Handles restoring state after output redirection as well as
        the actual pipe operation if present.

        :param statement: Statement object which contains the parsed input from the user
        :param saved_state: contains information needed to restore state data
        """
        if saved_state.redirecting:
            # If we redirected output to the clipboard
            if statement.output and not statement.output_to:
                self.stdout.seek(0)
                write_to_paste_buffer(self.stdout.read())

            try:
                # Close the file or pipe that stdout was redirected to
                self.stdout.close()
            except BrokenPipeError:
                pass

            # Restore the stdout values
            self.stdout = saved_state.saved_self_stdout
            sys.stdout = saved_state.saved_sys_stdout

            # Check if we need to wait for the process being piped to
            if self._cur_pipe_proc_reader is not None:
                self._cur_pipe_proc_reader.wait()

        # Restore _cur_pipe_proc_reader. This always is done, regardless of whether this command redirected.
        self._cur_pipe_proc_reader = saved_state.saved_pipe_proc_reader

    def cmd_func(self, command: str) -> Optional[Callable]:
        """
        Get the function for a command
        :param command: the name of the command
        """
        func_name = self._cmd_func_name(command)
        if func_name:
            return getattr(self, func_name)

    def _cmd_func_name(self, command: str) -> str:
        """Get the method name associated with a given command.

        :param command: command to look up method name which implements it
        :return: method name which implements the given command
        """
        target = COMMAND_FUNC_PREFIX + command
        return target if callable(getattr(self, target, None)) else ''

    # noinspection PyMethodOverriding
    def onecmd(self, statement: Union[Statement, str], *, add_to_history: bool = True) -> bool:
        """ This executes the actual do_* method for a command.

        If the command provided doesn't exist, then it executes default() instead.

        :param statement: intended to be a Statement instance parsed command from the input stream, alternative
                          acceptance of a str is present only for backward compatibility with cmd
        :param add_to_history: If True, then add this command to history. Defaults to True.
        :return: a flag indicating whether the interpretation of commands should stop
        """
        # For backwards compatibility with cmd, allow a str to be passed in
        if not isinstance(statement, Statement):
            statement = self._input_line_to_statement(statement)

        func = self.cmd_func(statement.command)
        if func:
            # Check to see if this command should be stored in history
            if statement.command not in self.exclude_from_history and \
                    statement.command not in self.disabled_commands and add_to_history:

                self.history.append(statement)

            stop = func(statement)

        else:
            stop = self.default(statement)

        if stop is None:
            stop = False

        return stop

    def default(self, statement: Statement) -> Optional[bool]:
        """Executed when the command given isn't a recognized command implemented by a do_* method.

        :param statement: Statement object with parsed input
        """
        if self.default_to_shell:
            if 'shell' not in self.exclude_from_history:
                self.history.append(statement)

            # noinspection PyTypeChecker
            return self.do_shell(statement.command_and_args)
        else:
            err_msg = self.default_error.format(statement.command)

            # Set apply_style to False so default_error's style is not overridden
            self.perror(err_msg, apply_style=False)

    def _pseudo_raw_input(self, prompt: str) -> str:
        """Began life as a copy of cmd's cmdloop; like raw_input but

        - accounts for changed stdin, stdout
        - if input is a pipe (instead of a tty), look at self.echo
          to decide whether to print the prompt and the input
        """
        if self.use_rawinput:
            try:
                if sys.stdin.isatty():
                    # Wrap in try since terminal_lock may not be locked when this function is called from unit tests
                    try:
                        # A prompt is about to be drawn. Allow asynchronous changes to the terminal.
                        self.terminal_lock.release()
                    except RuntimeError:
                        pass

                    # Deal with the vagaries of readline and ANSI escape codes
                    safe_prompt = rl_make_safe_prompt(prompt)
                    line = input(safe_prompt)
                else:
                    line = input()
                    if self.echo:
                        sys.stdout.write('{}{}\n'.format(prompt, line))
            except EOFError:
                line = 'eof'
            finally:
                if sys.stdin.isatty():
                    # The prompt is gone. Do not allow asynchronous changes to the terminal.
                    self.terminal_lock.acquire()
        else:
            if self.stdin.isatty():
                # on a tty, print the prompt first, then read the line
                self.poutput(prompt, end='')
                self.stdout.flush()
                line = self.stdin.readline()
                if len(line) == 0:
                    line = 'eof'
            else:
                # we are reading from a pipe, read the line to see if there is
                # anything there, if so, then decide whether to print the
                # prompt or not
                line = self.stdin.readline()
                if len(line):
                    # we read something, output the prompt and the something
                    if self.echo:
                        self.poutput('{}{}'.format(prompt, line))
                else:
                    line = 'eof'

        return line.rstrip('\r\n')

    def _set_up_cmd2_readline(self) -> _SavedReadlineSettings:
        """
        Set up readline with cmd2-specific settings
        :return: Class containing saved readline settings
        """
        readline_settings = _SavedReadlineSettings()

        if self.use_rawinput and self.completekey and rl_type != RlType.NONE:

            # Set up readline for our tab completion needs
            if rl_type == RlType.GNU:
                # Set GNU readline's rl_basic_quote_characters to NULL so it won't automatically add a closing quote
                # We don't need to worry about setting rl_completion_suppress_quote since we never declared
                # rl_completer_quote_characters.
                readline_settings.basic_quotes = ctypes.cast(rl_basic_quote_characters, ctypes.c_void_p).value
                rl_basic_quote_characters.value = None

            readline_settings.completer = readline.get_completer()
            readline.set_completer(self.complete)

            # Set the readline word delimiters for completion
            completer_delims = " \t\n"
            completer_delims += ''.join(constants.QUOTES)
            completer_delims += ''.join(constants.REDIRECTION_CHARS)
            completer_delims += ''.join(self.statement_parser.terminators)

            readline_settings.delims = readline.get_completer_delims()
            readline.set_completer_delims(completer_delims)

            # Enable tab completion
            readline.parse_and_bind(self.completekey + ": complete")

        return readline_settings

    def _restore_readline(self, readline_settings: _SavedReadlineSettings):
        """
        Restore saved readline settings
        :param readline_settings: the readline settings to restore
        """
        if self.use_rawinput and self.completekey and rl_type != RlType.NONE:

            # Restore what we changed in readline
            readline.set_completer(readline_settings.completer)
            readline.set_completer_delims(readline_settings.delims)

            if rl_type == RlType.GNU:
                readline.set_completion_display_matches_hook(None)
                rl_basic_quote_characters.value = readline_settings.basic_quotes
            elif rl_type == RlType.PYREADLINE:
                # noinspection PyUnresolvedReferences
                readline.rl.mode._display_completions = orig_pyreadline_display

    def _cmdloop(self) -> None:
        """Repeatedly issue a prompt, accept input, parse an initial prefix
        off the received input, and dispatch to action methods, passing them
        the remainder of the line as argument.

        This serves the same role as cmd.cmdloop().
        """
        saved_readline_settings = None

        try:
            # Get sigint protection while we set up readline for cmd2
            with self.sigint_protection:
                saved_readline_settings = self._set_up_cmd2_readline()

            # Run startup commands
            stop = self.runcmds_plus_hooks(self._startup_commands)
            self._startup_commands.clear()

            while not stop:
                # Get commands from user
                try:
                    line = self._pseudo_raw_input(self.prompt)
                except KeyboardInterrupt as ex:
                    if self.quit_on_sigint:
                        raise ex
                    else:
                        self.poutput('^C')
                        line = ''

                # Run the command along with all associated pre and post hooks
                stop = self.onecmd_plus_hooks(line)
        finally:
            # Get sigint protection while we restore readline settings
            with self.sigint_protection:
                if saved_readline_settings is not None:
                    self._restore_readline(saved_readline_settings)

    # -----  Alias subcommand functions -----

    def _alias_create(self, args: argparse.Namespace) -> None:
        """Create or overwrite an alias"""

        # Validate the alias name
        valid, errmsg = self.statement_parser.is_valid_command(args.name)
        if not valid:
            self.perror("Invalid alias name: {}".format(errmsg))
            return

        if args.name in self.get_all_commands():
            self.perror("Alias cannot have the same name as a command")
            return

        if args.name in self.macros:
            self.perror("Alias cannot have the same name as a macro")
            return

        # Unquote redirection and terminator tokens
        tokens_to_unquote = constants.REDIRECTION_TOKENS
        tokens_to_unquote.extend(self.statement_parser.terminators)
        utils.unquote_specific_tokens(args.command_args, tokens_to_unquote)

        # Build the alias value string
        value = args.command
        if args.command_args:
            value += ' ' + ' '.join(args.command_args)

        # Set the alias
        result = "overwritten" if args.name in self.aliases else "created"
        self.aliases[args.name] = value
        self.poutput("Alias '{}' {}".format(args.name, result))

    def _alias_delete(self, args: argparse.Namespace) -> None:
        """Delete aliases"""
        if args.all:
            self.aliases.clear()
            self.poutput("All aliases deleted")
        elif not args.name:
            self.perror("Either --all or alias name(s) must be specified")
        else:
            for cur_name in utils.remove_duplicates(args.name):
                if cur_name in self.aliases:
                    del self.aliases[cur_name]
                    self.poutput("Alias '{}' deleted".format(cur_name))
                else:
                    self.perror("Alias '{}' does not exist".format(cur_name))

    def _alias_list(self, args: argparse.Namespace) -> None:
        """List some or all aliases"""
        if args.name:
            for cur_name in utils.remove_duplicates(args.name):
                if cur_name in self.aliases:
                    self.poutput("alias create {} {}".format(cur_name, self.aliases[cur_name]))
                else:
                    self.perror("Alias '{}' not found".format(cur_name))
        else:
            for cur_alias in sorted(self.aliases, key=self.default_sort_key):
                self.poutput("alias create {} {}".format(cur_alias, self.aliases[cur_alias]))

    # Top-level parser for alias
    alias_description = ("Manage aliases\n"
                         "\n"
                         "An alias is a command that enables replacement of a word by another string.")
    alias_epilog = ("See also:\n"
                    "  macro")
    alias_parser = Cmd2ArgumentParser(description=alias_description, epilog=alias_epilog, prog='alias')

    # Add subcommands to alias
    alias_subparsers = alias_parser.add_subparsers()

    # alias -> create
    alias_create_help = "create or overwrite an alias"
    alias_create_description = "Create or overwrite an alias"

    alias_create_epilog = ("Notes:\n"
                           "  If you want to use redirection, pipes, or terminators in the value of the\n"
                           "  alias, then quote them.\n"
                           "\n"
                           "  Since aliases are resolved during parsing, tab completion will function as\n"
                           "  it would for the actual command the alias resolves to.\n"
                           "\n"
                           "Examples:\n"
                           "  alias create ls !ls -lF\n"
                           "  alias create show_log !cat \"log file.txt\"\n"
                           "  alias create save_results print_results \">\" out.txt\n")

    alias_create_parser = alias_subparsers.add_parser('create', help=alias_create_help,
                                                      description=alias_create_description,
                                                      epilog=alias_create_epilog)
    alias_create_parser.add_argument('name', help='name of this alias')
    alias_create_parser.add_argument('command', help='what the alias resolves to',
                                     choices_method=_get_commands_aliases_and_macros_for_completion)
    alias_create_parser.add_argument('command_args', nargs=argparse.REMAINDER, help='arguments to pass to command',
                                     completer_method=path_complete)
    alias_create_parser.set_defaults(func=_alias_create)

    # alias -> delete
    alias_delete_help = "delete aliases"
    alias_delete_description = "Delete specified aliases or all aliases if --all is used"
    alias_delete_parser = alias_subparsers.add_parser('delete', help=alias_delete_help,
                                                      description=alias_delete_description)
    alias_delete_parser.add_argument('name', nargs=argparse.ZERO_OR_MORE, help='alias to delete',
                                     choices_method=_get_alias_completion_items, descriptive_header='Value')
    alias_delete_parser.add_argument('-a', '--all', action='store_true', help="delete all aliases")
    alias_delete_parser.set_defaults(func=_alias_delete)

    # alias -> list
    alias_list_help = "list aliases"
    alias_list_description = ("List specified aliases in a reusable form that can be saved to a startup\n"
                              "script to preserve aliases across sessions\n"
                              "\n"
                              "Without arguments, all aliases will be listed.")

    alias_list_parser = alias_subparsers.add_parser('list', help=alias_list_help,
                                                    description=alias_list_description)
    alias_list_parser.add_argument('name', nargs=argparse.ZERO_OR_MORE, help='alias to list',
                                   choices_method=_get_alias_completion_items, descriptive_header='Value')
    alias_list_parser.set_defaults(func=_alias_list)

    # Preserve quotes since we are passing strings to other commands
    @with_argparser(alias_parser, preserve_quotes=True)
    def do_alias(self, args: argparse.Namespace) -> None:
        """Manage aliases"""
        func = getattr(args, 'func', None)
        if func is not None:
            # Call whatever subcommand function was selected
            func(self, args)
        else:
            # noinspection PyTypeChecker
            self.do_help('alias')

    # -----  Macro subcommand functions -----

    def _macro_create(self, args: argparse.Namespace) -> None:
        """Create or overwrite a macro"""

        # Validate the macro name
        valid, errmsg = self.statement_parser.is_valid_command(args.name)
        if not valid:
            self.perror("Invalid macro name: {}".format(errmsg))
            return

        if args.name in self.get_all_commands():
            self.perror("Macro cannot have the same name as a command")
            return

        if args.name in self.aliases:
            self.perror("Macro cannot have the same name as an alias")
            return

        # Unquote redirection and terminator tokens
        tokens_to_unquote = constants.REDIRECTION_TOKENS
        tokens_to_unquote.extend(self.statement_parser.terminators)
        utils.unquote_specific_tokens(args.command_args, tokens_to_unquote)

        # Build the macro value string
        value = args.command
        if args.command_args:
            value += ' ' + ' '.join(args.command_args)

        # Find all normal arguments
        arg_list = []
        normal_matches = re.finditer(MacroArg.macro_normal_arg_pattern, value)
        max_arg_num = 0
        arg_nums = set()

        while True:
            try:
                cur_match = normal_matches.__next__()

                # Get the number string between the braces
                cur_num_str = (re.findall(MacroArg.digit_pattern, cur_match.group())[0])
                cur_num = int(cur_num_str)
                if cur_num < 1:
                    self.perror("Argument numbers must be greater than 0")
                    return

                arg_nums.add(cur_num)
                if cur_num > max_arg_num:
                    max_arg_num = cur_num

                arg_list.append(MacroArg(start_index=cur_match.start(), number_str=cur_num_str, is_escaped=False))

            except StopIteration:
                break

        # Make sure the argument numbers are continuous
        if len(arg_nums) != max_arg_num:
            self.perror(
                "Not all numbers between 1 and {} are present "
                "in the argument placeholders".format(max_arg_num))
            return

        # Find all escaped arguments
        escaped_matches = re.finditer(MacroArg.macro_escaped_arg_pattern, value)

        while True:
            try:
                cur_match = escaped_matches.__next__()

                # Get the number string between the braces
                cur_num_str = re.findall(MacroArg.digit_pattern, cur_match.group())[0]

                arg_list.append(MacroArg(start_index=cur_match.start(), number_str=cur_num_str, is_escaped=True))
            except StopIteration:
                break

        # Set the macro
        result = "overwritten" if args.name in self.macros else "created"
        self.macros[args.name] = Macro(name=args.name, value=value, minimum_arg_count=max_arg_num, arg_list=arg_list)
        self.poutput("Macro '{}' {}".format(args.name, result))

    def _macro_delete(self, args: argparse.Namespace) -> None:
        """Delete macros"""
        if args.all:
            self.macros.clear()
            self.poutput("All macros deleted")
        elif not args.name:
            self.perror("Either --all or macro name(s) must be specified")
        else:
            for cur_name in utils.remove_duplicates(args.name):
                if cur_name in self.macros:
                    del self.macros[cur_name]
                    self.poutput("Macro '{}' deleted".format(cur_name))
                else:
                    self.perror("Macro '{}' does not exist".format(cur_name))

    def _macro_list(self, args: argparse.Namespace) -> None:
        """List some or all macros"""
        if args.name:
            for cur_name in utils.remove_duplicates(args.name):
                if cur_name in self.macros:
                    self.poutput("macro create {} {}".format(cur_name, self.macros[cur_name].value))
                else:
                    self.perror("Macro '{}' not found".format(cur_name))
        else:
            for cur_macro in sorted(self.macros, key=self.default_sort_key):
                self.poutput("macro create {} {}".format(cur_macro, self.macros[cur_macro].value))

    # Top-level parser for macro
    macro_description = ("Manage macros\n"
                         "\n"
                         "A macro is similar to an alias, but it can contain argument placeholders.")
    macro_epilog = ("See also:\n"
                    "  alias")
    macro_parser = Cmd2ArgumentParser(description=macro_description, epilog=macro_epilog, prog='macro')

    # Add subcommands to macro
    macro_subparsers = macro_parser.add_subparsers()

    # macro -> create
    macro_create_help = "create or overwrite a macro"
    macro_create_description = "Create or overwrite a macro"

    macro_create_epilog = ("A macro is similar to an alias, but it can contain argument placeholders.\n"
                           "Arguments are expressed when creating a macro using {#} notation where {1}\n"
                           "means the first argument.\n"
                           "\n"
                           "The following creates a macro called my_macro that expects two arguments:\n"
                           "\n"
                           "  macro create my_macro make_dinner --meat {1} --veggie {2}\n"
                           "\n"
                           "When the macro is called, the provided arguments are resolved and the\n"
                           "assembled command is run. For example:\n"
                           "\n"
                           "  my_macro beef broccoli ---> make_dinner --meat beef --veggie broccoli\n"
                           "\n"
                           "Notes:\n"
                           "  To use the literal string {1} in your command, escape it this way: {{1}}.\n"
                           "\n"
                           "  Extra arguments passed to a macro are appended to resolved command.\n"
                           "\n"
                           "  An argument number can be repeated in a macro. In the following example the\n"
                           "  first argument will populate both {1} instances.\n"
                           "\n"
                           "    macro create ft file_taxes -p {1} -q {2} -r {1}\n"
                           "\n"
                           "  To quote an argument in the resolved command, quote it during creation.\n"
                           "\n"
                           "    macro create backup !cp \"{1}\" \"{1}.orig\"\n"
                           "\n"
                           "  If you want to use redirection, pipes, or terminators in the value of the\n"
                           "  macro, then quote them.\n"
                           "\n"
                           "    macro create show_results print_results -type {1} \"|\" less\n"
                           "\n"
                           "  Because macros do not resolve until after hitting Enter, tab completion\n"
                           "  will only complete paths while typing a macro.")

    macro_create_parser = macro_subparsers.add_parser('create', help=macro_create_help,
                                                      description=macro_create_description,
                                                      epilog=macro_create_epilog)
    macro_create_parser.add_argument('name', help='name of this macro')
    macro_create_parser.add_argument('command', help='what the macro resolves to',
                                     choices_method=_get_commands_aliases_and_macros_for_completion)
    macro_create_parser.add_argument('command_args', nargs=argparse.REMAINDER,
                                     help='arguments to pass to command', completer_method=path_complete)
    macro_create_parser.set_defaults(func=_macro_create)

    # macro -> delete
    macro_delete_help = "delete macros"
    macro_delete_description = "Delete specified macros or all macros if --all is used"
    macro_delete_parser = macro_subparsers.add_parser('delete', help=macro_delete_help,
                                                      description=macro_delete_description)
    macro_delete_parser.add_argument('name', nargs=argparse.ZERO_OR_MORE, help='macro to delete',
                                     choices_method=_get_macro_completion_items, descriptive_header='Value')
    macro_delete_parser.add_argument('-a', '--all', action='store_true', help="delete all macros")
    macro_delete_parser.set_defaults(func=_macro_delete)

    # macro -> list
    macro_list_help = "list macros"
    macro_list_description = ("List specified macros in a reusable form that can be saved to a startup script\n"
                              "to preserve macros across sessions\n"
                              "\n"
                              "Without arguments, all macros will be listed.")

    macro_list_parser = macro_subparsers.add_parser('list', help=macro_list_help, description=macro_list_description)
    macro_list_parser.add_argument('name', nargs=argparse.ZERO_OR_MORE, help='macro to list',
                                   choices_method=_get_macro_completion_items, descriptive_header='Value')
    macro_list_parser.set_defaults(func=_macro_list)

    # Preserve quotes since we are passing strings to other commands
    @with_argparser(macro_parser, preserve_quotes=True)
    def do_macro(self, args: argparse.Namespace) -> None:
        """Manage macros"""
        func = getattr(args, 'func', None)
        if func is not None:
            # Call whatever subcommand function was selected
            func(self, args)
        else:
            # noinspection PyTypeChecker
            self.do_help('macro')

    def complete_help_command(self, text: str, line: str, begidx: int, endidx: int) -> List[str]:
        """Completes the command argument of help"""

        # Complete token against topics and visible commands
        topics = set(self.get_help_topics())
        visible_commands = set(self.get_visible_commands())
        strs_to_match = list(topics | visible_commands)
        return utils.basic_complete(text, line, begidx, endidx, strs_to_match)

    def complete_help_subcommand(self, text: str, line: str, begidx: int, endidx: int) -> List[str]:
        """Completes the subcommand argument of help"""

        # Get all tokens through the one being completed
        tokens, _ = self.tokens_for_completion(line, begidx, endidx)

        if not tokens:
            return []

        # Must have at least 3 args for 'help command subcommand'
        if len(tokens) < 3:
            return []

        # Find where the command is by skipping past any flags
        cmd_index = 1
        for cur_token in tokens[cmd_index:]:
            if not cur_token.startswith('-'):
                break
            cmd_index += 1

        if cmd_index >= len(tokens):
            return []

        command = tokens[cmd_index]
        matches = []

        # Check if this command uses argparse
        func = self.cmd_func(command)
        argparser = getattr(func, CMD_ATTR_ARGPARSER, None)

        if func is not None and argparser is not None:
            from .argparse_completer import AutoCompleter
            completer = AutoCompleter(argparser, self)
            matches = completer.complete_command_help(tokens[cmd_index:], text, line, begidx, endidx)

        return matches

    help_parser = Cmd2ArgumentParser(description="List available commands or provide "
                                                 "detailed help for a specific command")
    help_parser.add_argument('command', nargs=argparse.OPTIONAL, help="command to retrieve help for",
                             completer_method=complete_help_command)
    help_parser.add_argument('subcommand', nargs=argparse.REMAINDER, help="subcommand to retrieve help for",
                             completer_method=complete_help_subcommand)
    help_parser.add_argument('-v', '--verbose', action='store_true',
                             help="print a list of all commands with descriptions of each")

    # Get rid of cmd's complete_help() functions so AutoCompleter will complete the help command
    if getattr(cmd.Cmd, 'complete_help', None) is not None:
        delattr(cmd.Cmd, 'complete_help')

    @with_argparser(help_parser)
    def do_help(self, args: argparse.Namespace) -> None:
        """List available commands or provide detailed help for a specific command"""
        if not args.command or args.verbose:
            self._help_menu(args.verbose)

        else:
            # Getting help for a specific command
            func = self.cmd_func(args.command)
            help_func = getattr(self, HELP_FUNC_PREFIX + args.command, None)
            argparser = getattr(func, CMD_ATTR_ARGPARSER, None)

            # If the command function uses argparse, then use argparse's help
            if func is not None and argparser is not None:
                from .argparse_completer import AutoCompleter
                completer = AutoCompleter(argparser, self)
                tokens = [args.command] + args.subcommand

                # Set end to blank so the help output matches how it looks when "command -h" is used
                self.poutput(completer.format_help(tokens), end='')

            # If there is no help information then print an error
            elif help_func is None and (func is None or not func.__doc__):
                err_msg = self.help_error.format(args.command)

                # Set apply_style to False so help_error's style is not overridden
                self.perror(err_msg, apply_style=False)

            # Otherwise delegate to cmd base class do_help()
            else:
                super().do_help(args.command)

    def _help_menu(self, verbose: bool = False) -> None:
        """Show a list of commands which help can be displayed for.
        """
        # Get a sorted list of help topics
        help_topics = sorted(self.get_help_topics(), key=self.default_sort_key)

        # Get a sorted list of visible command names
        visible_commands = sorted(self.get_visible_commands(), key=self.default_sort_key)

        cmds_doc = []
        cmds_undoc = []
        cmds_cats = {}

        for command in visible_commands:
            func = self.cmd_func(command)
            has_help_func = False

            if command in help_topics:
                # Prevent the command from showing as both a command and help topic in the output
                help_topics.remove(command)

                # Non-argparse commands can have help_functions for their documentation
                if not hasattr(func, CMD_ATTR_ARGPARSER):
                    has_help_func = True

            if hasattr(func, CMD_ATTR_HELP_CATEGORY):
                category = getattr(func, CMD_ATTR_HELP_CATEGORY)
                cmds_cats.setdefault(category, [])
                cmds_cats[category].append(command)
            elif func.__doc__ or has_help_func:
                cmds_doc.append(command)
            else:
                cmds_undoc.append(command)

        if len(cmds_cats) == 0:
            # No categories found, fall back to standard behavior
            self.poutput("{}".format(str(self.doc_leader)))
            self._print_topics(self.doc_header, cmds_doc, verbose)
        else:
            # Categories found, Organize all commands by category
            self.poutput('{}'.format(str(self.doc_leader)))
            self.poutput('{}'.format(str(self.doc_header)), end="\n\n")
            for category in sorted(cmds_cats.keys(), key=self.default_sort_key):
                self._print_topics(category, cmds_cats[category], verbose)
            self._print_topics(self.default_category, cmds_doc, verbose)

        self.print_topics(self.misc_header, help_topics, 15, 80)
        self.print_topics(self.undoc_header, cmds_undoc, 15, 80)

    def _print_topics(self, header: str, cmds: List[str], verbose: bool) -> None:
        """Customized version of print_topics that can switch between verbose or traditional output"""
        import io

        if cmds:
            if not verbose:
                self.print_topics(header, cmds, 15, 80)
            else:
                self.stdout.write('{}\n'.format(str(header)))
                widest = 0
                # measure the commands
                for command in cmds:
                    width = ansi.ansi_safe_wcswidth(command)
                    if width > widest:
                        widest = width
                # add a 4-space pad
                widest += 4
                if widest < 20:
                    widest = 20

                if self.ruler:
                    self.stdout.write('{:{ruler}<{width}}\n'.format('', ruler=self.ruler, width=80))

                # Try to get the documentation string for each command
                topics = self.get_help_topics()

                for command in cmds:
                    cmd_func = self.cmd_func(command)

                    # Non-argparse commands can have help_functions for their documentation
                    if not hasattr(cmd_func, CMD_ATTR_ARGPARSER) and command in topics:
                        help_func = getattr(self, HELP_FUNC_PREFIX + command)
                        result = io.StringIO()

                        # try to redirect system stdout
                        with redirect_stdout(result):
                            # save our internal stdout
                            stdout_orig = self.stdout
                            try:
                                # redirect our internal stdout
                                self.stdout = result
                                help_func()
                            finally:
                                # restore internal stdout
                                self.stdout = stdout_orig
                        doc = result.getvalue()

                    else:
                        doc = cmd_func.__doc__

                    # Attempt to locate the first documentation block
                    if not doc:
                        doc_block = ['']
                    else:
                        doc_block = []
                        found_first = False
                        for doc_line in doc.splitlines():
                            stripped_line = doc_line.strip()

                            # Don't include :param type lines
                            if stripped_line.startswith(':'):
                                if found_first:
                                    break
                            elif stripped_line:
                                doc_block.append(stripped_line)
                                found_first = True
                            elif found_first:
                                break

                    for doc_line in doc_block:
                        self.stdout.write('{: <{col_width}}{doc}\n'.format(command,
                                                                           col_width=widest,
                                                                           doc=doc_line))
                        command = ''
                self.stdout.write("\n")

    @with_argparser(Cmd2ArgumentParser(description="List available shortcuts"))
    def do_shortcuts(self, _: argparse.Namespace) -> None:
        """List available shortcuts"""
        # Sort the shortcut tuples by name
        sorted_shortcuts = sorted(self.statement_parser.shortcuts, key=lambda x: self.default_sort_key(x[0]))
        result = "\n".join('{}: {}'.format(sc[0], sc[1]) for sc in sorted_shortcuts)
        self.poutput("Shortcuts for other commands:\n{}".format(result))

    @with_argparser(Cmd2ArgumentParser(epilog=INTERNAL_COMMAND_EPILOG))
    def do_eof(self, _: argparse.Namespace) -> bool:
        """Called when <Ctrl>-D is pressed"""
        # Return True to stop the command loop
        return True

    @with_argparser(Cmd2ArgumentParser(description="Exit this application"))
    def do_quit(self, _: argparse.Namespace) -> bool:
        """Exit this application"""
        # Return True to stop the command loop
        return True

    def select(self, opts: Union[str, List[str], List[Tuple[Any, Optional[str]]]],
               prompt: str = 'Your choice? ') -> str:
        """Presents a numbered menu to the user.  Modeled after
           the bash shell's SELECT.  Returns the item chosen.

           Argument ``opts`` can be:

             | a single string -> will be split into one-word options
             | a list of strings -> will be offered as options
             | a list of tuples -> interpreted as (value, text), so
                                   that the return value can differ from
                                   the text advertised to the user """

        completion_disabled = False
        orig_completer = None

        def disable_completion():
            """Turn off completion during the select input line"""
            nonlocal orig_completer
            nonlocal completion_disabled

            if rl_type != RlType.NONE and not completion_disabled:
                orig_completer = readline.get_completer()
                readline.set_completer(lambda *args, **kwargs: None)
                completion_disabled = True

        def enable_completion():
            """Restore tab completion when select is done reading input"""
            nonlocal completion_disabled

            if rl_type != RlType.NONE and completion_disabled:
                readline.set_completer(orig_completer)
                completion_disabled = False

        local_opts = opts
        if isinstance(opts, str):
            local_opts = list(zip(opts.split(), opts.split()))
        fulloptions = []
        for opt in local_opts:
            if isinstance(opt, str):
                fulloptions.append((opt, opt))
            else:
                try:
                    fulloptions.append((opt[0], opt[1]))
                except IndexError:
                    fulloptions.append((opt[0], opt[0]))
        for (idx, (_, text)) in enumerate(fulloptions):
            self.poutput('  %2d. %s' % (idx + 1, text))

        while True:
            safe_prompt = rl_make_safe_prompt(prompt)

            try:
                with self.sigint_protection:
                    disable_completion()
                response = input(safe_prompt)
            except EOFError:
                response = ''
                self.poutput('\n', end='')
            finally:
                with self.sigint_protection:
                    enable_completion()

            if not response:
                continue

            if rl_type != RlType.NONE:
                hlen = readline.get_current_history_length()
                if hlen >= 1:
                    readline.remove_history_item(hlen - 1)
            try:
                choice = int(response)
                if choice < 1:
                    raise IndexError
                result = fulloptions[choice - 1][0]
                break
            except (ValueError, IndexError):
                self.poutput("{!r} isn't a valid choice. Pick a number between 1 and {}:".format(
                    response, len(fulloptions)))

        return result

    def _get_read_only_settings(self) -> str:
        """Return a summary report of read-only settings which the user cannot modify at runtime.

        :return: The report string
        """
        read_only_settings = """
        Commands may be terminated with: {}
        Output redirection and pipes allowed: {}"""
        return read_only_settings.format(str(self.statement_parser.terminators), self.allow_redirection)

    def _show(self, args: argparse.Namespace, parameter: str = '') -> None:
        """Shows current settings of parameters.

        :param args: argparse parsed arguments from the set command
        :param parameter: optional search parameter
        """
        param = utils.norm_fold(parameter.strip())
        result = {}
        maxlen = 0

        for p in self.settable:
            if (not param) or p.startswith(param):
                result[p] = '{}: {}'.format(p, str(getattr(self, p)))
                maxlen = max(maxlen, len(result[p]))

        if result:
            for p in sorted(result, key=self.default_sort_key):
                if args.long:
                    self.poutput('{} # {}'.format(result[p].ljust(maxlen), self.settable[p]))
                else:
                    self.poutput(result[p])

            # If user has requested to see all settings, also show read-only settings
            if args.all:
                self.poutput('\nRead only settings:{}'.format(self._get_read_only_settings()))
        else:
            self.perror("Parameter '{}' not supported (type 'set' for list of parameters).".format(param))

    set_description = ("Set a settable parameter or show current settings of parameters\n"
                       "\n"
                       "Accepts abbreviated parameter names so long as there is no ambiguity.\n"
                       "Call without arguments for a list of settable parameters with their values.")

    set_parser = Cmd2ArgumentParser(description=set_description)
    set_parser.add_argument('-a', '--all', action='store_true', help='display read-only settings as well')
    set_parser.add_argument('-l', '--long', action='store_true', help='describe function of parameter')
    set_parser.add_argument('param', nargs=argparse.OPTIONAL, help='parameter to set or view',
                            choices_method=_get_settable_completion_items, descriptive_header='Description')
    set_parser.add_argument('value', nargs=argparse.OPTIONAL, help='the new value for settable')

    @with_argparser(set_parser)
    def do_set(self, args: argparse.Namespace) -> None:
        """Set a settable parameter or show current settings of parameters"""

        # Check if param was passed in
        if not args.param:
            return self._show(args)
        param = utils.norm_fold(args.param.strip())

        # Check if value was passed in
        if not args.value:
            return self._show(args, param)
        value = args.value

        # Check if param points to just one settable
        if param not in self.settable:
            hits = [p for p in self.settable if p.startswith(param)]
            if len(hits) == 1:
                param = hits[0]
            else:
                return self._show(args, param)

        # Update the settable's value
        orig_value = getattr(self, param)
        setattr(self, param, utils.cast(orig_value, value))

        # In cases where a Python property is used to validate and update a settable's value, its value will not
        # change if the passed in one is invalid. Therefore we should read its actual value back and not assume.
        new_value = getattr(self, param)

        self.poutput('{} - was: {}\nnow: {}'.format(param, orig_value, new_value))

        # See if we need to call a change hook for this settable
        if orig_value != new_value:
            onchange_hook = getattr(self, '_onchange_{}'.format(param), None)
            if onchange_hook is not None:
                onchange_hook(old=orig_value, new=new_value)  # pylint: disable=not-callable

    shell_parser = Cmd2ArgumentParser(description="Execute a command as if at the OS prompt")
    shell_parser.add_argument('command', help='the command to run', completer_method=shell_cmd_complete)
    shell_parser.add_argument('command_args', nargs=argparse.REMAINDER, help='arguments to pass to command',
                              completer_method=path_complete)

    # Preserve quotes since we are passing these strings to the shell
    @with_argparser(shell_parser, preserve_quotes=True)
    def do_shell(self, args: argparse.Namespace) -> None:
        """Execute a command as if at the OS prompt"""
        import subprocess

        # Create a list of arguments to shell
        tokens = [args.command] + args.command_args

        # Expand ~ where needed
        utils.expand_user_in_tokens(tokens)
        expanded_command = ' '.join(tokens)

        # Prevent KeyboardInterrupts while in the shell process. The shell process will
        # still receive the SIGINT since it is in the same process group as us.
        with self.sigint_protection:
            # For any stream that is a StdSim, we will use a pipe so we can capture its output
            proc = subprocess.Popen(expanded_command,
                                    stdout=subprocess.PIPE if isinstance(self.stdout, utils.StdSim) else self.stdout,
                                    stderr=subprocess.PIPE if isinstance(sys.stderr, utils.StdSim) else sys.stderr,
                                    shell=True)

            proc_reader = utils.ProcReader(proc, self.stdout, sys.stderr)
            proc_reader.wait()

    @staticmethod
    def _reset_py_display() -> None:
        """
        Resets the dynamic objects in the sys module that the py and ipy consoles fight over.
        When a Python console starts it adopts certain display settings if they've already been set.
        If an ipy console has previously been run, then py uses its settings and ends up looking
        like an ipy console in terms of prompt and exception text. This method forces the Python
        console to create its own display settings since they won't exist.

        IPython does not have this problem since it always overwrites the display settings when it
        is run. Therefore this method only needs to be called before creating a Python console.
        """
        # Delete any prompts that have been set
        attributes = ['ps1', 'ps2', 'ps3']
        for cur_attr in attributes:
            try:
                del sys.__dict__[cur_attr]
            except KeyError:
                pass

        # Reset functions
        sys.displayhook = sys.__displayhook__
        sys.excepthook = sys.__excepthook__

    def _set_up_py_shell_env(self, interp: InteractiveConsole) -> _SavedCmd2Env:
        """
        Set up interactive Python shell environment
        :return: Class containing saved up cmd2 environment
        """
        cmd2_env = _SavedCmd2Env()

        # Set up readline for Python shell
        if rl_type != RlType.NONE:
            # Save cmd2 history
            for i in range(1, readline.get_current_history_length() + 1):
                # noinspection PyArgumentList
                cmd2_env.history.append(readline.get_history_item(i))

            readline.clear_history()

            # Restore py's history
            for item in self._py_history:
                readline.add_history(item)

            if self.use_rawinput and self.completekey:
                # Set up tab completion for the Python console
                # rlcompleter relies on the default settings of the Python readline module
                if rl_type == RlType.GNU:
                    cmd2_env.readline_settings.basic_quotes = ctypes.cast(rl_basic_quote_characters,
                                                                          ctypes.c_void_p).value
                    rl_basic_quote_characters.value = orig_rl_basic_quotes

                    if 'gnureadline' in sys.modules:
                        # rlcompleter imports readline by name, so it won't use gnureadline
                        # Force rlcompleter to use gnureadline instead so it has our settings and history
                        if 'readline' in sys.modules:
                            cmd2_env.readline_module = sys.modules['readline']

                        sys.modules['readline'] = sys.modules['gnureadline']

                cmd2_env.readline_settings.delims = readline.get_completer_delims()
                readline.set_completer_delims(orig_rl_delims)

                # rlcompleter will not need cmd2's custom display function
                # This will be restored by cmd2 the next time complete() is called
                if rl_type == RlType.GNU:
                    readline.set_completion_display_matches_hook(None)
                elif rl_type == RlType.PYREADLINE:
                    # noinspection PyUnresolvedReferences
                    readline.rl.mode._display_completions = orig_pyreadline_display

                # Save off the current completer and set a new one in the Python console
                # Make sure it tab completes from its locals() dictionary
                cmd2_env.readline_settings.completer = readline.get_completer()
                interp.runcode("from rlcompleter import Completer")
                interp.runcode("import readline")
                interp.runcode("readline.set_completer(Completer(locals()).complete)")

        # Set up sys module for the Python console
        self._reset_py_display()

        cmd2_env.sys_stdout = sys.stdout
        sys.stdout = self.stdout

        cmd2_env.sys_stdin = sys.stdin
        sys.stdin = self.stdin

        return cmd2_env

    def _restore_cmd2_env(self, cmd2_env: _SavedCmd2Env) -> None:
        """
        Restore cmd2 environment after exiting an interactive Python shell
        :param cmd2_env: the environment settings to restore
        """
        sys.stdout = cmd2_env.sys_stdout
        sys.stdin = cmd2_env.sys_stdin

        # Set up readline for cmd2
        if rl_type != RlType.NONE:
            # Save py's history
            self._py_history.clear()
            for i in range(1, readline.get_current_history_length() + 1):
                # noinspection PyArgumentList
                self._py_history.append(readline.get_history_item(i))

            readline.clear_history()

            # Restore cmd2's history
            for item in cmd2_env.history:
                readline.add_history(item)

            if self.use_rawinput and self.completekey:
                # Restore cmd2's tab completion settings
                readline.set_completer(cmd2_env.readline_settings.completer)
                readline.set_completer_delims(cmd2_env.readline_settings.delims)

                if rl_type == RlType.GNU:
                    rl_basic_quote_characters.value = cmd2_env.readline_settings.basic_quotes

                    if 'gnureadline' in sys.modules:
                        # Restore what the readline module pointed to
                        if cmd2_env.readline_module is None:
                            del(sys.modules['readline'])
                        else:
                            sys.modules['readline'] = cmd2_env.readline_module

    py_description = ("Invoke Python command or shell\n"
                      "\n"
                      "Note that, when invoking a command directly from the command line, this shell\n"
                      "has limited ability to parse Python statements into tokens. In particular,\n"
                      "there may be problems with whitespace and quotes depending on their placement.\n"
                      "\n"
                      "If you see strange parsing behavior, it's best to just open the Python shell\n"
                      "by providing no arguments to py and run more complex statements there.")

    py_parser = Cmd2ArgumentParser(description=py_description)
    py_parser.add_argument('command', nargs=argparse.OPTIONAL, help="command to run")
    py_parser.add_argument('remainder', nargs=argparse.REMAINDER, help="remainder of command")

    # This is a hidden flag for telling do_py to run a pyscript. It is intended only to be used by run_pyscript
    # after it sets up sys.argv for the script being run. When this flag is present, it takes precedence over all
    # other arguments. run_pyscript uses this method instead of "py run('file')" because file names with
    # 2 or more consecutive spaces cause issues with our parser, which isn't meant to parse Python statements.
    py_parser.add_argument('--pyscript', help=argparse.SUPPRESS)

    # Preserve quotes since we are passing these strings to Python
    @with_argparser(py_parser, preserve_quotes=True)
    def do_py(self, args: argparse.Namespace) -> Optional[bool]:
        """
        Enter an interactive Python shell
        :return: True if running of commands should stop
        """
        from .py_bridge import PyBridge
        if self._in_py:
            err = "Recursively entering interactive Python consoles is not allowed."
            self.perror(err)
            return

        py_bridge = PyBridge(self)
        py_code_to_run = ''

        # Handle case where we were called by run_pyscript
        if args.pyscript:
            args.pyscript = utils.strip_quotes(args.pyscript)

            # Run the script - use repr formatting to escape things which
            # need to be escaped to prevent issues on Windows
            py_code_to_run = 'run({!r})'.format(args.pyscript)

        elif args.command:
            py_code_to_run = args.command
            if args.remainder:
                py_code_to_run += ' ' + ' '.join(args.remainder)

            # Set cmd_echo to True so PyBridge statements like: py app('help')
            # run at the command line will print their output.
            py_bridge.cmd_echo = True

        try:
            self._in_py = True

            def py_run(filename: str):
                """Run a Python script file in the interactive console.
                :param filename: filename of script file to run
                """
                expanded_filename = os.path.expanduser(filename)

                try:
                    with open(expanded_filename) as f:
                        interp.runcode(f.read())
                except OSError as ex:
                    self.pexcept("Error reading script file '{}': {}".format(expanded_filename, ex))

            def py_quit():
                """Function callable from the interactive Python console to exit that environment"""
                raise EmbeddedConsoleExit

            # Set up Python environment
            self.py_locals[self.py_bridge_name] = py_bridge
            self.py_locals['run'] = py_run
            self.py_locals['quit'] = py_quit
            self.py_locals['exit'] = py_quit

            if self.locals_in_py:
                self.py_locals['self'] = self
            elif 'self' in self.py_locals:
                del self.py_locals['self']

            localvars = self.py_locals
            interp = InteractiveConsole(locals=localvars)
            interp.runcode('import sys, os;sys.path.insert(0, os.getcwd())')

            # Check if we are running Python code
            if py_code_to_run:
                # noinspection PyBroadException
                try:
                    interp.runcode(py_code_to_run)
                except BaseException:
                    # We don't care about any exception that happened in the Python code
                    pass

            # Otherwise we will open an interactive Python shell
            else:
                cprt = 'Type "help", "copyright", "credits" or "license" for more information.'
                instructions = ('End with `Ctrl-D` (Unix) / `Ctrl-Z` (Windows), `quit()`, `exit()`.\n'
                                'Non-Python commands can be issued with: {}("your command")\n'
                                'Run Python code from external script files with: run("script.py")'
                                .format(self.py_bridge_name))

                saved_cmd2_env = None

                # noinspection PyBroadException
                try:
                    # Get sigint protection while we set up the Python shell environment
                    with self.sigint_protection:
                        saved_cmd2_env = self._set_up_py_shell_env(interp)

                    interp.interact(banner="Python {} on {}\n{}\n\n{}\n".
                                    format(sys.version, sys.platform, cprt, instructions))
                except BaseException:
                    # We don't care about any exception that happened in the interactive console
                    pass

                finally:
                    # Get sigint protection while we restore cmd2 environment settings
                    with self.sigint_protection:
                        if saved_cmd2_env is not None:
                            self._restore_cmd2_env(saved_cmd2_env)

        except KeyboardInterrupt:
            pass

        finally:
            self._in_py = False

        return py_bridge.stop

    run_pyscript_parser = Cmd2ArgumentParser(description="Run a Python script file inside the console")
    run_pyscript_parser.add_argument('script_path', help='path to the script file', completer_method=path_complete)
    run_pyscript_parser.add_argument('script_arguments', nargs=argparse.REMAINDER,
                                     help='arguments to pass to script', completer_method=path_complete)

    @with_argparser(run_pyscript_parser)
    def do_run_pyscript(self, args: argparse.Namespace) -> Optional[bool]:
        """
        Run a Python script file inside the console
         :return: True if running of commands should stop
        """
        # Expand ~ before placing this path in sys.argv just as a shell would
        args.script_path = os.path.expanduser(args.script_path)

        # Add some protection against accidentally running a non-Python file. The happens when users
        # mix up run_script and run_pyscript.
        if not args.script_path.endswith('.py'):
            self.pwarning("'{}' does not have a .py extension".format(args.script_path))
            selection = self.select('Yes No', 'Continue to try to run it as a Python script? ')
            if selection != 'Yes':
                return

        py_return = False

        # Save current command line arguments
        orig_args = sys.argv

        try:
            # Overwrite sys.argv to allow the script to take command line arguments
            sys.argv = [args.script_path] + args.script_arguments

            # noinspection PyTypeChecker
            py_return = self.do_py('--pyscript {}'.format(utils.quote_string(args.script_path)))

        except KeyboardInterrupt:
            pass

        finally:
            # Restore command line arguments to original state
            sys.argv = orig_args

        return py_return

    # Only include the do_ipy() method if IPython is available on the system
    if ipython_available:  # pragma: no cover
        @with_argparser(Cmd2ArgumentParser(description="Enter an interactive IPython shell"))
        def do_ipy(self, _: argparse.Namespace) -> None:
            """Enter an interactive IPython shell"""
            from .py_bridge import PyBridge
            banner = ('Entering an embedded IPython shell. Type quit or <Ctrl>-d to exit.\n'
                      'Run Python code from external files with: run filename.py\n')
            exit_msg = 'Leaving IPython, back to {}'.format(sys.argv[0])

            def load_ipy(cmd2_app: Cmd, py_bridge: PyBridge):
                """
                Embed an IPython shell in an environment that is restricted to only the variables in this function
                :param cmd2_app: instance of the cmd2 app
                :param py_bridge: a PyscriptBridge
                """
                # Create a variable pointing to py_bridge and name it using the value of py_bridge_name
                exec("{} = py_bridge".format(cmd2_app.py_bridge_name))

                # Add self variable pointing to cmd2_app, if allowed
                if cmd2_app.locals_in_py:
                    exec("self = cmd2_app")

                # Delete these names from the environment so IPython can't use them
                del cmd2_app
                del py_bridge

                embed(banner1=banner, exit_msg=exit_msg)

            load_ipy(self, PyBridge(self))

    history_description = "View, run, edit, save, or clear previously entered commands"

    history_parser = Cmd2ArgumentParser(description=history_description)
    history_action_group = history_parser.add_mutually_exclusive_group()
    history_action_group.add_argument('-r', '--run', action='store_true', help='run selected history items')
    history_action_group.add_argument('-e', '--edit', action='store_true',
                                      help='edit and then run selected history items')
    history_action_group.add_argument('-o', '--output_file', metavar='FILE',
                                      help='output commands to a script file, implies -s',
                                      completer_method=path_complete)
    history_action_group.add_argument('-t', '--transcript', metavar='TRANSCRIPT_FILE',
                                      help='output commands and results to a transcript file,\nimplies -s',
                                      completer_method=path_complete)
    history_action_group.add_argument('-c', '--clear', action='store_true', help='clear all history')

    history_format_group = history_parser.add_argument_group(title='formatting')
    history_format_group.add_argument('-s', '--script', action='store_true',
                                      help='output commands in script format, i.e. without command\n'
                                           'numbers')
    history_format_group.add_argument('-x', '--expanded', action='store_true',
                                      help='output fully parsed commands with any aliases and\n'
                                           'macros expanded, instead of typed commands')
    history_format_group.add_argument('-v', '--verbose', action='store_true',
                                      help='display history and include expanded commands if they\n'
                                           'differ from the typed command')
    history_format_group.add_argument('-a', '--all', action='store_true',
                                      help='display all commands, including ones persisted from\n'
                                           'previous sessions')

    history_arg_help = ("empty               all history items\n"
                        "a                   one history item by number\n"
                        "a..b, a:b, a:, ..b  items by indices (inclusive)\n"
                        "string              items containing string\n"
                        "/regex/             items matching regular expression")
    history_parser.add_argument('arg', nargs=argparse.OPTIONAL, help=history_arg_help)

    @with_argparser(history_parser)
    def do_history(self, args: argparse.Namespace) -> Optional[bool]:
        """
        View, run, edit, save, or clear previously entered commands
        :return: True if running of commands should stop
        """

        # -v must be used alone with no other options
        if args.verbose:
            if args.clear or args.edit or args.output_file or args.run or args.transcript \
                    or args.expanded or args.script:
                self.poutput("-v can not be used with any other options")
                self.poutput(self.history_parser.format_usage())
                return

        # -s and -x can only be used if none of these options are present: [-c -r -e -o -t]
        if (args.script or args.expanded) \
                and (args.clear or args.edit or args.output_file or args.run or args.transcript):
            self.poutput("-s and -x can not be used with -c, -r, -e, -o, or -t")
            self.poutput(self.history_parser.format_usage())
            return

        if args.clear:
            # Clear command and readline history
            self.history.clear()

            if self.persistent_history_file:
                os.remove(self.persistent_history_file)

            if rl_type != RlType.NONE:
                readline.clear_history()
            return

        # If an argument was supplied, then retrieve partial contents of the history
        cowardly_refuse_to_run = False
        if args.arg:
            # If a character indicating a slice is present, retrieve
            # a slice of the history
            arg = args.arg
            arg_is_int = False
            try:
                int(arg)
                arg_is_int = True
            except ValueError:
                pass

            if '..' in arg or ':' in arg:
                # Get a slice of history
                history = self.history.span(arg, args.all)
            elif arg_is_int:
                history = [self.history.get(arg)]
            elif arg.startswith(r'/') and arg.endswith(r'/'):
                history = self.history.regex_search(arg, args.all)
            else:
                history = self.history.str_search(arg, args.all)
        else:
            # If no arg given, then retrieve the entire history
            cowardly_refuse_to_run = True
            # Get a copy of the history so it doesn't get mutated while we are using it
            history = self.history.span(':', args.all)

        if args.run:
            if cowardly_refuse_to_run:
                self.perror("Cowardly refusing to run all previously entered commands.")
                self.perror("If this is what you want to do, specify '1:' as the range of history.")
            else:
                return self.runcmds_plus_hooks(history)
        elif args.edit:
            import tempfile
            fd, fname = tempfile.mkstemp(suffix='.txt', text=True)
            with os.fdopen(fd, 'w') as fobj:
                for command in history:
                    if command.statement.multiline_command:
                        fobj.write('{}\n'.format(command.expanded.rstrip()))
                    else:
                        fobj.write('{}\n'.format(command.raw))
            try:
                # Handle potential edge case where the temp file needs to be quoted on the command line
                quoted_fname = utils.quote_string(fname)

                # noinspection PyTypeChecker
                self.do_edit(quoted_fname)

                # noinspection PyTypeChecker
                self.do_run_script(quoted_fname)
            finally:
                os.remove(fname)
        elif args.output_file:
            try:
                with open(os.path.expanduser(args.output_file), 'w') as fobj:
                    for item in history:
                        if item.statement.multiline_command:
                            fobj.write('{}\n'.format(item.expanded.rstrip()))
                        else:
                            fobj.write('{}\n'.format(item.raw))
                plural = 's' if len(history) > 1 else ''
            except OSError as e:
                self.pexcept('Error saving {!r} - {}'.format(args.output_file, e))
            else:
                self.pfeedback('{} command{} saved to {}'.format(len(history), plural, args.output_file))
        elif args.transcript:
            self._generate_transcript(history, args.transcript)
        else:
            # Display the history items retrieved
            for hi in history:
                self.poutput(hi.pr(script=args.script, expanded=args.expanded, verbose=args.verbose))

    def _initialize_history(self, hist_file):
        """Initialize history using history related attributes

        This function can determine whether history is saved in the prior text-based
        format (one line of input is stored as one line in the file), or the new-as-
        of-version 0.9.13 pickle based format.

        History created by versions <= 0.9.12 is in readline format, i.e. plain text files.

        Initializing history does not effect history files on disk, versions >= 0.9.13 always
        write history in the pickle format.
        """
        self.history = History()
        # with no persistent history, nothing else in this method is relevant
        if not hist_file:
            self.persistent_history_file = hist_file
            return

        hist_file = os.path.abspath(os.path.expanduser(hist_file))

        # on Windows, trying to open a directory throws a permission
        # error, not a `IsADirectoryError`. So we'll check it ourselves.
        if os.path.isdir(hist_file):
            msg = "Persistent history file '{}' is a directory"
            self.perror(msg.format(hist_file))
            return

        # Create the directory for the history file if it doesn't already exist
        hist_file_dir = os.path.dirname(hist_file)
        try:
            os.makedirs(hist_file_dir, exist_ok=True)
        except OSError as ex:
            msg = "Error creating persistent history file directory '{}': {}".format(hist_file_dir, ex)
            self.pexcept(msg)
            return

        # first we try and unpickle the history file
        history = History()

        try:
            with open(hist_file, 'rb') as fobj:
                history = pickle.load(fobj)
        except (AttributeError, EOFError, FileNotFoundError, ImportError, IndexError, KeyError, pickle.UnpicklingError):
            # If any non-operating system error occurs when attempting to unpickle, just use an empty history
            pass
        except OSError as ex:
            msg = "Can not read persistent history file '{}': {}"
            self.pexcept(msg.format(hist_file, ex))
            return

        self.history = history
        self.history.start_session()
        self.persistent_history_file = hist_file

        # populate readline history
        if rl_type != RlType.NONE:
            last = None
            for item in history:
                # Break the command into its individual lines
                for line in item.raw.splitlines():
                    # readline only adds a single entry for multiple sequential identical lines
                    # so we emulate that behavior here
                    if line != last:
                        readline.add_history(line)
                        last = line

        # register a function to write history at save
        # if the history file is in plain text format from 0.9.12 or lower
        # this will fail, and the history in the plain text file will be lost
        import atexit
        atexit.register(self._persist_history)

    def _persist_history(self):
        """write history out to the history file"""
        if not self.persistent_history_file:
            return

        self.history.truncate(self._persistent_history_length)
        try:
            with open(self.persistent_history_file, 'wb') as fobj:
                pickle.dump(self.history, fobj)
        except OSError as ex:
            msg = "Can not write persistent history file '{}': {}"
            self.pexcept(msg.format(self.persistent_history_file, ex))

    def _generate_transcript(self, history: List[Union[HistoryItem, str]], transcript_file: str) -> None:
        """
        Generate a transcript file from a given history of commands
        """
        # Validate the transcript file path to make sure directory exists and write access is available
        transcript_path = os.path.abspath(os.path.expanduser(transcript_file))
        transcript_dir = os.path.dirname(transcript_path)
        if not os.path.isdir(transcript_dir) or not os.access(transcript_dir, os.W_OK):
            self.perror("{!r} is not a directory or you don't have write access".format(transcript_dir))
            return

        commands_run = 0
        try:
            with self.sigint_protection:
                # Disable echo while we manually redirect stdout to a StringIO buffer
                saved_echo = self.echo
                saved_stdout = self.stdout
                self.echo = False

            # The problem with supporting regular expressions in transcripts
            # is that they shouldn't be processed in the command, just the output.
            # In addition, when we generate a transcript, any slashes in the output
            # are not really intended to indicate regular expressions, so they should
            # be escaped.
            #
            # We have to jump through some hoops here in order to catch the commands
            # separately from the output and escape the slashes in the output.
            transcript = ''
            for history_item in history:
                # build the command, complete with prompts. When we replay
                # the transcript, we look for the prompts to separate
                # the command from the output
                first = True
                command = ''
                if isinstance(history_item, HistoryItem):
                    history_item = history_item.raw
                for line in history_item.splitlines():
                    if first:
                        command += '{}{}\n'.format(self.prompt, line)
                        first = False
                    else:
                        command += '{}{}\n'.format(self.continuation_prompt, line)
                transcript += command

                # Use a StdSim object to capture output
                self.stdout = utils.StdSim(self.stdout)

                # then run the command and let the output go into our buffer
                stop = self.onecmd_plus_hooks(history_item)
                commands_run += 1

                # add the regex-escaped output to the transcript
                transcript += self.stdout.getvalue().replace('/', r'\/')

                # check if we are supposed to stop
                if stop:
                    break
        finally:
            with self.sigint_protection:
                # Restore altered attributes to their original state
                self.echo = saved_echo
                self.stdout = saved_stdout

        # Check if all commands ran
        if commands_run < len(history):
            warning = "Command {} triggered a stop and ended transcript generation early".format(commands_run)
            self.pwarning(warning)

        # finally, we can write the transcript out to the file
        try:
            with open(transcript_file, 'w') as fout:
                fout.write(transcript)
        except OSError as ex:
            self.pexcept('Failed to save transcript: {}'.format(ex))
        else:
            # and let the user know what we did
            if commands_run > 1:
                plural = 'commands and their outputs'
            else:
                plural = 'command and its output'
            msg = '{} {} saved to transcript file {!r}'
            self.pfeedback(msg.format(commands_run, plural, transcript_file))

    edit_description = ("Edit a file in a text editor\n"
                        "\n"
                        "The editor used is determined by a settable parameter. To set it:\n"
                        "\n"
                        "  set editor (program-name)")

    edit_parser = Cmd2ArgumentParser(description=edit_description)
    edit_parser.add_argument('file_path', nargs=argparse.OPTIONAL,
                             help="path to a file to open in editor", completer_method=path_complete)

    @with_argparser(edit_parser)
    def do_edit(self, args: argparse.Namespace) -> None:
        """Edit a file in a text editor"""
        if not self.editor:
            raise EnvironmentError("Please use 'set editor' to specify your text editing program of choice.")

        command = utils.quote_string(os.path.expanduser(self.editor))
        if args.file_path:
            command += " " + utils.quote_string(os.path.expanduser(args.file_path))

        # noinspection PyTypeChecker
        self.do_shell(command)

    @property
    def _current_script_dir(self) -> Optional[str]:
        """Accessor to get the current script directory from the _script_dir LIFO queue."""
        if self._script_dir:
            return self._script_dir[-1]
        else:
            return None

    run_script_description = ("Run commands in script file that is encoded as either ASCII or UTF-8 text\n"
                              "\n"
                              "Script should contain one command per line, just like the command would be\n"
                              "typed in the console.\n"
                              "\n"
                              "If the -t/--transcript flag is used, this command instead records\n"
                              "the output of the script commands to a transcript for testing purposes.\n")

    run_script_parser = Cmd2ArgumentParser(description=run_script_description)
    run_script_parser.add_argument('-t', '--transcript', metavar='TRANSCRIPT_FILE',
                                   help='record the output of the script as a transcript file',
                                   completer_method=path_complete)
    run_script_parser.add_argument('script_path', help="path to the script file", completer_method=path_complete)

    @with_argparser(run_script_parser)
    def do_run_script(self, args: argparse.Namespace) -> Optional[bool]:
        """Run commands in script file that is encoded as either ASCII or UTF-8 text.

        :return: True if running of commands should stop
        """
        expanded_path = os.path.abspath(os.path.expanduser(args.script_path))

        # Make sure the path exists and we can access it
        if not os.path.exists(expanded_path):
            self.perror("'{}' does not exist or cannot be accessed".format(expanded_path))
            return

        # Make sure expanded_path points to a file
        if not os.path.isfile(expanded_path):
            self.perror("'{}' is not a file".format(expanded_path))
            return

        # An empty file is not an error, so just return
        if os.path.getsize(expanded_path) == 0:
            return

        # Make sure the file is ASCII or UTF-8 encoded text
        if not utils.is_text_file(expanded_path):
            self.perror("'{}' is not an ASCII or UTF-8 encoded text file".format(expanded_path))
            return

        # Add some protection against accidentally running a Python file. The happens when users
        # mix up run_script and run_pyscript.
        if expanded_path.endswith('.py'):
            self.pwarning("'{}' appears to be a Python file".format(expanded_path))
            selection = self.select('Yes No', 'Continue to try to run it as a text script? ')
            if selection != 'Yes':
                return

        try:
            # Read all lines of the script
            with open(expanded_path, encoding='utf-8') as target:
                script_commands = target.read().splitlines()
        except OSError as ex:  # pragma: no cover
            self.pexcept("Problem accessing script from '{}': {}".format(expanded_path, ex))
            return

        orig_script_dir_count = len(self._script_dir)

        try:
            self._script_dir.append(os.path.dirname(expanded_path))

            if args.transcript:
                self._generate_transcript(script_commands, os.path.expanduser(args.transcript))
            else:
                return self.runcmds_plus_hooks(script_commands)

        finally:
            with self.sigint_protection:
                # Check if a script dir was added before an exception occurred
                if orig_script_dir_count != len(self._script_dir):
                    self._script_dir.pop()

    relative_run_script_description = run_script_description
    relative_run_script_description += (
        "\n\n"
        "If this is called from within an already-running script, the filename will be\n"
        "interpreted relative to the already-running script's directory.")

    relative_run_script_epilog = ("Notes:\n"
                                  "  This command is intended to only be used within text file scripts.")

    relative_run_script_parser = Cmd2ArgumentParser(description=relative_run_script_description,
                                                    epilog=relative_run_script_epilog)
    relative_run_script_parser.add_argument('file_path', help='a file path pointing to a script')

    @with_argparser(relative_run_script_parser)
    def do__relative_run_script(self, args: argparse.Namespace) -> Optional[bool]:
        """
        Run commands in script file that is encoded as either ASCII or UTF-8 text
        :return: True if running of commands should stop
        """
        file_path = args.file_path
        # NOTE: Relative path is an absolute path, it is just relative to the current script directory
        relative_path = os.path.join(self._current_script_dir or '', file_path)

        # noinspection PyTypeChecker
        return self.do_run_script(utils.quote_string(relative_path))

    def _run_transcript_tests(self, transcript_paths: List[str]) -> None:
        """Runs transcript tests for provided file(s).

        This is called when either -t is provided on the command line or the transcript_files argument is provided
        during construction of the cmd2.Cmd instance.

        :param transcript_paths: list of transcript test file paths
        """
        import time
        import unittest
        import cmd2
        from .transcript import Cmd2TestCase

        class TestMyAppCase(Cmd2TestCase):
            cmdapp = self

        # Validate that there is at least one transcript file
        transcripts_expanded = utils.files_from_glob_patterns(transcript_paths, access=os.R_OK)
        if not transcripts_expanded:
            self.perror('No test files found - nothing to test')
            self.exit_code = -1
            return

        verinfo = ".".join(map(str, sys.version_info[:3]))
        num_transcripts = len(transcripts_expanded)
        plural = '' if len(transcripts_expanded) == 1 else 's'
        self.poutput(ansi.style(utils.center_text('cmd2 transcript test', pad='='), bold=True))
        self.poutput('platform {} -- Python {}, cmd2-{}, readline-{}'.format(sys.platform, verinfo, cmd2.__version__,
                                                                             rl_type))
        self.poutput('cwd: {}'.format(os.getcwd()))
        self.poutput('cmd2 app: {}'.format(sys.argv[0]))
        self.poutput(ansi.style('collected {} transcript{}'.format(num_transcripts, plural), bold=True))

        self.__class__.testfiles = transcripts_expanded
        sys.argv = [sys.argv[0]]  # the --test argument upsets unittest.main()
        testcase = TestMyAppCase()
        stream = utils.StdSim(sys.stderr)
        # noinspection PyTypeChecker
        runner = unittest.TextTestRunner(stream=stream)
        start_time = time.time()
        test_results = runner.run(testcase)
        execution_time = time.time() - start_time
        if test_results.wasSuccessful():
            ansi.ansi_aware_write(sys.stderr, stream.read())
            finish_msg = '{0} transcript{1} passed in {2:.3f} seconds'.format(num_transcripts, plural, execution_time)
            finish_msg = ansi.style_success(utils.center_text(finish_msg, pad='='))
            self.poutput(finish_msg)
        else:
            # Strip off the initial traceback which isn't particularly useful for end users
            error_str = stream.read()
            end_of_trace = error_str.find('AssertionError:')
            file_offset = error_str[end_of_trace:].find('File ')
            start = end_of_trace + file_offset

            # But print the transcript file name and line number followed by what was expected and what was observed
            self.perror(error_str[start:])

            # Return a failure error code to support automated transcript-based testing
            self.exit_code = -1

    def async_alert(self, alert_msg: str, new_prompt: Optional[str] = None) -> None:  # pragma: no cover
        """
        Display an important message to the user while they are at the prompt in between commands.
        To the user it appears as if an alert message is printed above the prompt and their current input
        text and cursor location is left alone.

        Raises a `RuntimeError` if called while another thread holds `terminal_lock`.

        IMPORTANT: This function will not print an alert unless it can acquire self.terminal_lock to ensure
                   a prompt is onscreen.  Therefore it is best to acquire the lock before calling this function
                   to guarantee the alert prints.

        :param alert_msg: the message to display to the user
        :param new_prompt: if you also want to change the prompt that is displayed, then include it here
                           see async_update_prompt() docstring for guidance on updating a prompt
        """
        if not (vt100_support and self.use_rawinput):
            return

        # Sanity check that can't fail if self.terminal_lock was acquired before calling this function
        if self.terminal_lock.acquire(blocking=False):

            # Figure out what prompt is displaying
            current_prompt = self.continuation_prompt if self._at_continuation_prompt else self.prompt

            # Only update terminal if there are changes
            update_terminal = False

            if alert_msg:
                alert_msg += '\n'
                update_terminal = True

            # Set the prompt if its changed
            if new_prompt is not None and new_prompt != self.prompt:
                self.prompt = new_prompt

                # If we aren't at a continuation prompt, then it's OK to update it
                if not self._at_continuation_prompt:
                    rl_set_prompt(self.prompt)
                    update_terminal = True

            if update_terminal:
                import shutil
                terminal_str = ansi.async_alert_str(terminal_columns=shutil.get_terminal_size().columns,
                                                    prompt=current_prompt, line=readline.get_line_buffer(),
                                                    cursor_offset=rl_get_point(), alert_msg=alert_msg)
                if rl_type == RlType.GNU:
                    sys.stderr.write(terminal_str)
                elif rl_type == RlType.PYREADLINE:
                    # noinspection PyUnresolvedReferences
                    readline.rl.mode.console.write(terminal_str)

                # Redraw the prompt and input lines
                rl_force_redisplay()

            self.terminal_lock.release()

        else:
            raise RuntimeError("another thread holds terminal_lock")

    def async_update_prompt(self, new_prompt: str) -> None:  # pragma: no cover
        """
        Update the prompt while the user is still typing at it. This is good for alerting the user to system
        changes dynamically in between commands. For instance you could alter the color of the prompt to indicate
        a system status or increase a counter to report an event. If you do alter the actual text of the prompt,
        it is best to keep the prompt the same width as what's on screen. Otherwise the user's input text will
        be shifted and the update will not be seamless.

        Raises a `RuntimeError` if called while another thread holds `terminal_lock`.

        IMPORTANT: This function will not update the prompt unless it can acquire self.terminal_lock to ensure
                   a prompt is onscreen.  Therefore it is best to acquire the lock before calling this function
                   to guarantee the prompt changes.

                   If a continuation prompt is currently being displayed while entering a multiline
                   command, the onscreen prompt will not change. However self.prompt will still be updated
                   and display immediately after the multiline line command completes.

        :param new_prompt: what to change the prompt to
        """
        self.async_alert('', new_prompt)

    def set_window_title(self, title: str) -> None:  # pragma: no cover
        """Set the terminal window title.

        Raises a `RuntimeError` if called while another thread holds `terminal_lock`.

        IMPORTANT: This function will not set the title unless it can acquire self.terminal_lock to avoid
                   writing to stderr while a command is running. Therefore it is best to acquire the lock
                   before calling this function to guarantee the title changes.

        :param title: the new window title
        """
        if not vt100_support:
            return

        # Sanity check that can't fail if self.terminal_lock was acquired before calling this function
        if self.terminal_lock.acquire(blocking=False):
            try:
                sys.stderr.write(ansi.set_title_str(title))
            except AttributeError:
                # Debugging in Pycharm has issues with setting terminal title
                pass
            finally:
                self.terminal_lock.release()

        else:
            raise RuntimeError("another thread holds terminal_lock")

    def enable_command(self, command: str) -> None:
        """
        Enable a command by restoring its functions
        :param command: the command being enabled
        """
        # If the commands is already enabled, then return
        if command not in self.disabled_commands:
            return

        help_func_name = HELP_FUNC_PREFIX + command
        completer_func_name = COMPLETER_FUNC_PREFIX + command

        # Restore the command function to its original value
        dc = self.disabled_commands[command]
        setattr(self, self._cmd_func_name(command), dc.command_function)

        # Restore the help function to its original value
        if dc.help_function is None:
            delattr(self, help_func_name)
        else:
            setattr(self, help_func_name, dc.help_function)

        # Restore the completer function to its original value
        if dc.completer_function is None:
            delattr(self, completer_func_name)
        else:
            setattr(self, completer_func_name, dc.completer_function)

        # Remove the disabled command entry
        del self.disabled_commands[command]

    def enable_category(self, category: str) -> None:
        """
        Enable an entire category of commands
        :param category: the category to enable
        """
        for cmd_name in list(self.disabled_commands):
            func = self.disabled_commands[cmd_name].command_function
            if getattr(func, CMD_ATTR_HELP_CATEGORY, None) == category:
                self.enable_command(cmd_name)

    def disable_command(self, command: str, message_to_print: str) -> None:
        """
        Disable a command and overwrite its functions
        :param command: the command being disabled
        :param message_to_print: what to print when this command is run or help is called on it while disabled

                                 The variable COMMAND_NAME can be used as a placeholder for the name of the
                                 command being disabled.
                                 ex: message_to_print = "{} is currently disabled".format(COMMAND_NAME)
        """
        import functools

        # If the commands is already disabled, then return
        if command in self.disabled_commands:
            return

        # Make sure this is an actual command
        command_function = self.cmd_func(command)
        if command_function is None:
            raise AttributeError("{} does not refer to a command".format(command))

        help_func_name = HELP_FUNC_PREFIX + command
        completer_func_name = COMPLETER_FUNC_PREFIX + command

        # Add the disabled command record
        self.disabled_commands[command] = DisabledCommand(command_function=command_function,
                                                          help_function=getattr(self, help_func_name, None),
                                                          completer_function=getattr(self, completer_func_name, None))

        # Overwrite the command and help functions to print the message
        new_func = functools.partial(self._report_disabled_command_usage,
                                     message_to_print=message_to_print.replace(COMMAND_NAME, command))
        setattr(self, self._cmd_func_name(command), new_func)
        setattr(self, help_func_name, new_func)

        # Set the completer to a function that returns a blank list
        setattr(self, completer_func_name, lambda *args, **kwargs: [])

    def disable_category(self, category: str, message_to_print: str) -> None:
        """Disable an entire category of commands.

        :param category: the category to disable
        :param message_to_print: what to print when anything in this category is run or help is called on it
                                 while disabled. The variable COMMAND_NAME can be used as a placeholder for the name
                                 of the command being disabled.
                                 ex: message_to_print = "{} is currently disabled".format(COMMAND_NAME)
        """
        all_commands = self.get_all_commands()

        for cmd_name in all_commands:
            func = self.cmd_func(cmd_name)
            if getattr(func, CMD_ATTR_HELP_CATEGORY, None) == category:
                self.disable_command(cmd_name, message_to_print)

    # noinspection PyUnusedLocal
    def _report_disabled_command_usage(self, *args, message_to_print: str, **kwargs) -> None:
        """
        Report when a disabled command has been run or had help called on it
        :param args: not used
        :param message_to_print: the message reporting that the command is disabled
        :param kwargs: not used
        """
        # Set apply_style to False so message_to_print's style is not overridden
        self.perror(message_to_print, apply_style=False)

    def cmdloop(self, intro: Optional[str] = None) -> int:
        """This is an outer wrapper around _cmdloop() which deals with extra features provided by cmd2.

        _cmdloop() provides the main loop equivalent to cmd.cmdloop().  This is a wrapper around that which deals with
        the following extra features provided by cmd2:
        - transcript testing
        - intro banner
        - exit code

        :param intro: if provided this overrides self.intro and serves as the intro banner printed once at start
        """
        # cmdloop() expects to be run in the main thread to support extensive use of KeyboardInterrupts throughout the
        # other built-in functions. You are free to override cmdloop, but much of cmd2's features will be limited.
        if not threading.current_thread() is threading.main_thread():
            raise RuntimeError("cmdloop must be run in the main thread")

        # Register a SIGINT signal handler for Ctrl+C
        import signal
        original_sigint_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, self.sigint_handler)

        # Grab terminal lock before the prompt has been drawn by readline
        self.terminal_lock.acquire()

        # Always run the preloop first
        for func in self._preloop_hooks:
            func()
        self.preloop()

        # If transcript-based regression testing was requested, then do that instead of the main loop
        if self._transcript_files is not None:
            self._run_transcript_tests([os.path.expanduser(tf) for tf in self._transcript_files])
        else:
            # If an intro was supplied in the method call, allow it to override the default
            if intro is not None:
                self.intro = intro

            # Print the intro, if there is one, right after the preloop
            if self.intro is not None:
                self.poutput(self.intro)

            # And then call _cmdloop() to enter the main loop
            self._cmdloop()

        # Run the postloop() no matter what
        for func in self._postloop_hooks:
            func()
        self.postloop()

        # Release terminal lock now that postloop code should have stopped any terminal updater threads
        # This will also zero the lock count in case cmdloop() is called again
        self.terminal_lock.release()

        # Restore the original signal handler
        signal.signal(signal.SIGINT, original_sigint_handler)

        return self.exit_code

    ###
    #
    # plugin related functions
    #
    ###
    def _initialize_plugin_system(self) -> None:
        """Initialize the plugin system"""
        self._preloop_hooks = []
        self._postloop_hooks = []
        self._postparsing_hooks = []
        self._precmd_hooks = []
        self._postcmd_hooks = []
        self._cmdfinalization_hooks = []

    @classmethod
    def _validate_callable_param_count(cls, func: Callable, count: int) -> None:
        """Ensure a function has the given number of parameters."""
        signature = inspect.signature(func)
        # validate that the callable has the right number of parameters
        nparam = len(signature.parameters)
        if nparam != count:
            raise TypeError('{} has {} positional arguments, expected {}'.format(
                func.__name__,
                nparam,
                count,
            ))

    @classmethod
    def _validate_prepostloop_callable(cls, func: Callable[[None], None]) -> None:
        """Check parameter and return types for preloop and postloop hooks."""
        cls._validate_callable_param_count(func, 0)
        # make sure there is no return notation
        signature = inspect.signature(func)
        if signature.return_annotation is not None:
            raise TypeError("{} must declare return a return type of 'None'".format(
                func.__name__,
            ))

    def register_preloop_hook(self, func: Callable[[None], None]) -> None:
        """Register a function to be called at the beginning of the command loop."""
        self._validate_prepostloop_callable(func)
        self._preloop_hooks.append(func)

    def register_postloop_hook(self, func: Callable[[None], None]) -> None:
        """Register a function to be called at the end of the command loop."""
        self._validate_prepostloop_callable(func)
        self._postloop_hooks.append(func)

    @classmethod
    def _validate_postparsing_callable(cls, func: Callable[[plugin.PostparsingData], plugin.PostparsingData]) -> None:
        """Check parameter and return types for postparsing hooks"""
        cls._validate_callable_param_count(func, 1)
        signature = inspect.signature(func)
        _, param = list(signature.parameters.items())[0]
        if param.annotation != plugin.PostparsingData:
            raise TypeError("{} must have one parameter declared with type 'cmd2.plugin.PostparsingData'".format(
                func.__name__
            ))
        if signature.return_annotation != plugin.PostparsingData:
            raise TypeError("{} must declare return a return type of 'cmd2.plugin.PostparsingData'".format(
                func.__name__
            ))

    def register_postparsing_hook(self, func: Callable[[plugin.PostparsingData], plugin.PostparsingData]) -> None:
        """Register a function to be called after parsing user input but before running the command"""
        self._validate_postparsing_callable(func)
        self._postparsing_hooks.append(func)

    @classmethod
    def _validate_prepostcmd_hook(cls, func: Callable, data_type: Type) -> None:
        """Check parameter and return types for pre and post command hooks."""
        signature = inspect.signature(func)
        # validate that the callable has the right number of parameters
        cls._validate_callable_param_count(func, 1)
        # validate the parameter has the right annotation
        paramname = list(signature.parameters.keys())[0]
        param = signature.parameters[paramname]
        if param.annotation != data_type:
            raise TypeError('argument 1 of {} has incompatible type {}, expected {}'.format(
                func.__name__,
                param.annotation,
                data_type,
            ))
        # validate the return value has the right annotation
        if signature.return_annotation == signature.empty:
            raise TypeError('{} does not have a declared return type, expected {}'.format(
                func.__name__,
                data_type,
            ))
        if signature.return_annotation != data_type:
            raise TypeError('{} has incompatible return type {}, expected {}'.format(
                func.__name__,
                signature.return_annotation,
                data_type,
            ))

    def register_precmd_hook(self, func: Callable[[plugin.PrecommandData], plugin.PrecommandData]) -> None:
        """Register a hook to be called before the command function."""
        self._validate_prepostcmd_hook(func, plugin.PrecommandData)
        self._precmd_hooks.append(func)

    def register_postcmd_hook(self, func: Callable[[plugin.PostcommandData], plugin.PostcommandData]) -> None:
        """Register a hook to be called after the command function."""
        self._validate_prepostcmd_hook(func, plugin.PostcommandData)
        self._postcmd_hooks.append(func)

    @classmethod
    def _validate_cmdfinalization_callable(cls, func: Callable[[plugin.CommandFinalizationData],
                                                               plugin.CommandFinalizationData]) -> None:
        """Check parameter and return types for command finalization hooks."""
        cls._validate_callable_param_count(func, 1)
        signature = inspect.signature(func)
        _, param = list(signature.parameters.items())[0]
        if param.annotation != plugin.CommandFinalizationData:
            raise TypeError("{} must have one parameter declared with type "
                            "'cmd2.plugin.CommandFinalizationData'".format(func.__name__))
        if signature.return_annotation != plugin.CommandFinalizationData:
            raise TypeError("{} must declare return a return type of "
                            "'cmd2.plugin.CommandFinalizationData'".format(func.__name__))

    def register_cmdfinalization_hook(self, func: Callable[[plugin.CommandFinalizationData],
                                                           plugin.CommandFinalizationData]) -> None:
        """Register a hook to be called after a command is completed, whether it completes successfully or not."""
        self._validate_cmdfinalization_callable(func)
        self._cmdfinalization_hooks.append(func)
