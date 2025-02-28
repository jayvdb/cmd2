# coding=utf-8
# flake8: noqa E302
"""
Unit/functional testing for argparse completer in cmd2
"""
import argparse
from typing import List

import pytest

import cmd2
from cmd2 import with_argparser, Cmd2ArgumentParser, CompletionItem
from cmd2.utils import StdSim, basic_complete
from .conftest import run_cmd, complete_tester

# Lists used in our tests (there is a mix of sorted and unsorted on purpose)
static_int_choices_list = [-1, 1, -2, 2, 0, -12]
static_choices_list = ['static', 'choices', 'stop', 'here']
choices_from_function = ['choices', 'function', 'chatty', 'smith']
choices_from_method = ['choices', 'method', 'most', 'improved']

set_value_choices = ['set', 'value', 'choices']
one_or_more_choices = ['one', 'or', 'more', 'choices']
optional_choices = ['a', 'few', 'optional', 'choices']
range_choices = ['some', 'range', 'choices']
remainder_choices = ['remainder', 'choices']

positional_choices = ['the', 'positional', 'choices']

completions_from_function = ['completions', 'function', 'fairly', 'complete']
completions_from_method = ['completions', 'method', 'missed', 'spot']


def choices_function() -> List[str]:
    """Function that provides choices"""
    return choices_from_function


def completer_function(text: str, line: str, begidx: int, endidx: int) -> List[str]:
    """Tab completion function"""
    return basic_complete(text, line, begidx, endidx, completions_from_function)


# noinspection PyMethodMayBeStatic,PyUnusedLocal
class AutoCompleteTester(cmd2.Cmd):
    """Cmd2 app that exercises AutoCompleter class"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    ############################################################################################################
    # Begin code related to help and command name completion
    ############################################################################################################
    def _music_create(self, args: argparse.Namespace) -> None:
        """Implements the 'music create' command"""
        self.poutput('music create')

    def _music_create_jazz(self, args: argparse.Namespace) -> None:
        """Implements the 'music create jazz' command"""
        self.poutput('music create jazz')

    def _music_create_rock(self, args: argparse.Namespace) -> None:
        """Implements the 'music create rock' command"""
        self.poutput('music create rock')

    # Top level parser for music command
    music_parser = Cmd2ArgumentParser(description='Manage music', prog='music')

    # Add subcommands to music
    music_subparsers = music_parser.add_subparsers()

    # music -> create
    music_create_parser = music_subparsers.add_parser('create', help='Create music')
    music_create_parser.set_defaults(func=_music_create)

    # Add subcommands to music -> create
    music_create_subparsers = music_create_parser.add_subparsers()

    # music -> create -> jazz
    music_create_jazz_parser = music_create_subparsers.add_parser('jazz', help='Create jazz')
    music_create_jazz_parser.set_defaults(func=_music_create_jazz)

    # music -> create -> rock
    music_create_rock_parser = music_create_subparsers.add_parser('rock', help='Create rocks')
    music_create_rock_parser.set_defaults(func=_music_create_rock)

    @with_argparser(music_parser)
    def do_music(self, args: argparse.Namespace) -> None:
        """Music command"""
        func = getattr(args, 'func', None)
        if func is not None:
            # Call whatever subcommand function was selected
            func(self, args)
        else:
            # No subcommand was provided, so call help
            # noinspection PyTypeChecker
            self.do_help('music')

    ############################################################################################################
    # Begin code related to flag completion
    ############################################################################################################

    # Uses default flag prefix value (-)
    flag_parser = Cmd2ArgumentParser()
    flag_parser.add_argument('-n', '--normal_flag', help='A normal flag', action='store_true')
    flag_parser.add_argument('-a', '--append_flag', help='Append flag', action='append')
    flag_parser.add_argument('-o', '--append_const_flag', help='Append const flag', action='append_const', const=True)
    flag_parser.add_argument('-c', '--count_flag', help='Count flag', action='count')
    flag_parser.add_argument('-s', '--suppressed_flag', help=argparse.SUPPRESS, action='store_true')
    flag_parser.add_argument('-r', '--remainder_flag', nargs=argparse.REMAINDER, help='a remainder flag')

    @with_argparser(flag_parser)
    def do_flag(self, args: argparse.Namespace) -> None:
        pass

    # Uses non-default flag prefix value (+)
    plus_flag_parser = Cmd2ArgumentParser(prefix_chars='+')
    plus_flag_parser.add_argument('+n', '++normal_flag', help='A normal flag', action='store_true')

    @with_argparser(plus_flag_parser)
    def do_plus_flag(self, args: argparse.Namespace) -> None:
        pass

    ############################################################################################################
    # Begin code related to testing choices, choices_function, and choices_method parameters
    ############################################################################################################
    def choices_method(self) -> List[str]:
        """Method that provides choices"""
        return choices_from_method

    def completion_item_method(self) -> List[CompletionItem]:
        """Choices method that returns CompletionItems"""
        items = []
        for i in range(0, 10):
            main_str = 'main_str{}'.format(i)
            items.append(CompletionItem(main_str, desc='blah blah'))
        return items

    choices_parser = Cmd2ArgumentParser()

    # Flag args for choices command. Include string and non-string arg types.
    choices_parser.add_argument("-l", "--list", help="a flag populated with a choices list",
                                choices=static_choices_list)
    choices_parser.add_argument("-f", "--function", help="a flag populated with a choices function",
                                choices_function=choices_function)
    choices_parser.add_argument("-m", "--method", help="a flag populated with a choices method",
                                choices_method=choices_method)
    choices_parser.add_argument('-n', "--no_header", help='this arg has a no descriptive header',
                                choices_method=completion_item_method)
    choices_parser.add_argument('-i', '--int', type=int, help='a flag with an int type',
                                choices=static_int_choices_list)

    # Positional args for choices command
    choices_parser.add_argument("list_pos", help="a positional populated with a choices list",
                                choices=static_choices_list)
    choices_parser.add_argument("function_pos", help="a positional populated with a choices function",
                                choices_function=choices_function)
    choices_parser.add_argument("method_pos", help="a positional populated with a choices method",
                                choices_method=choices_method)

    @with_argparser(choices_parser)
    def do_choices(self, args: argparse.Namespace) -> None:
        pass

    ############################################################################################################
    # Begin code related to testing completer_function and completer_method parameters
    ############################################################################################################
    def completer_method(self, text: str, line: str, begidx: int, endidx: int) -> List[str]:
        """Tab completion method"""
        return basic_complete(text, line, begidx, endidx, completions_from_method)

    completer_parser = Cmd2ArgumentParser()

    # Flag args for completer command
    completer_parser.add_argument("-f", "--function", help="a flag using a completer function",
                                  completer_function=completer_function)
    completer_parser.add_argument("-m", "--method", help="a flag using a completer method",
                                  completer_method=completer_method)

    # Positional args for completer command
    completer_parser.add_argument("function_pos", help="a positional using a completer function",
                                  completer_function=completer_function)
    completer_parser.add_argument("method_pos", help="a positional using a completer method",
                                  completer_method=completer_method)

    @with_argparser(completer_parser)
    def do_completer(self, args: argparse.Namespace) -> None:
        pass

    ############################################################################################################
    # Begin code related to nargs
    ############################################################################################################
    nargs_parser = Cmd2ArgumentParser()

    # Flag args for nargs command
    nargs_parser.add_argument("--set_value", help="a flag with a set value for nargs", nargs=2,
                              choices=set_value_choices)
    nargs_parser.add_argument("--one_or_more", help="a flag wanting one or more args", nargs=argparse.ONE_OR_MORE,
                              choices=one_or_more_choices)
    nargs_parser.add_argument("--optional", help="a flag with an optional value", nargs=argparse.OPTIONAL,
                              choices=optional_choices)
    nargs_parser.add_argument("--range", help="a flag with nargs range", nargs=(1, 2),
                              choices=range_choices)
    nargs_parser.add_argument("--remainder", help="a flag wanting remaining", nargs=argparse.REMAINDER,
                              choices=remainder_choices)

    nargs_parser.add_argument("normal_pos", help="a remainder positional", nargs=2,
                              choices=positional_choices)
    nargs_parser.add_argument("remainder_pos", help="a remainder positional", nargs=argparse.REMAINDER,
                              choices=remainder_choices)

    @with_argparser(nargs_parser)
    def do_nargs(self, args: argparse.Namespace) -> None:
        pass

    ############################################################################################################
    # Begin code related to testing tab hints
    ############################################################################################################
    hint_parser = Cmd2ArgumentParser()
    hint_parser.add_argument('-f', '--flag', help='a flag arg')
    hint_parser.add_argument('-s', '--suppressed_help', help=argparse.SUPPRESS)
    hint_parser.add_argument('-t', '--suppressed_hint', help='a flag arg', suppress_tab_hint=True)

    hint_parser.add_argument('hint_pos', help='here is a hint\nwith new lines')
    hint_parser.add_argument('no_help_pos')

    @with_argparser(hint_parser)
    def do_hint(self, args: argparse.Namespace) -> None:
        pass


@pytest.fixture
def ac_app():
    app = AutoCompleteTester()
    app.stdout = StdSim(app.stdout)
    return app


@pytest.mark.parametrize('command', [
    'music',
    'music create',
    'music create rock',
    'music create jazz'
])
def test_help(ac_app, command):
    out1, err1 = run_cmd(ac_app, '{} -h'.format(command))
    out2, err2 = run_cmd(ac_app, 'help {}'.format(command))
    assert out1 == out2


@pytest.mark.parametrize('command, text, completions', [
    ('', 'mu', ['music ']),
    ('music', 'cre', ['create ']),
    ('music', 'creab', []),
    ('music create', '', ['jazz', 'rock']),
    ('music crea', 'jazz', []),
    ('music create', 'foo', [])
])
def test_complete_help(ac_app, command, text, completions):
    line = 'help {} {}'.format(command, text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


@pytest.mark.parametrize('subcommand, text, completions', [
    ('create', '', ['jazz', 'rock']),
    ('create', 'ja', ['jazz ']),
    ('create', 'foo', []),
    ('creab', 'ja', [])
])
def test_subcommand_completions(ac_app, subcommand, text, completions):
    line = 'music {} {}'.format(subcommand, text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


@pytest.mark.parametrize('command_and_args, text, completions', [
    # Complete all flags (suppressed will not show)
    ('flag', '-', ['--append_const_flag', '--append_flag', '--count_flag', '--help', '--normal_flag',
                   '--remainder_flag', '-a', '-c', '-h', '-n', '-o', '-r']),
    ('flag', '--', ['--append_const_flag', '--append_flag', '--count_flag', '--help',
                    '--normal_flag', '--remainder_flag']),

    # Complete individual flag
    ('flag', '-n', ['-n ']),
    ('flag', '--n', ['--normal_flag ']),

    # No flags should complete until current flag has its args
    ('flag --append_flag', '-', []),

    # Complete REMAINDER flag name
    ('flag', '-r', ['-r ']),
    ('flag', '--r', ['--remainder_flag ']),

    # No flags after a REMAINDER should complete
    ('flag -r value', '-', []),
    ('flag --remainder_flag value', '--', []),

    # Suppressed flag should not complete
    ('flag', '-s', []),
    ('flag', '--s', []),

    # A used flag should not show in completions
    ('flag -n', '--', ['--append_const_flag', '--append_flag', '--count_flag', '--help', '--remainder_flag']),

    # Flags with actions set to append, append_const, and count will always show even if they've been used
    ('flag --append_const_flag -c --append_flag value', '--', ['--append_const_flag', '--append_flag', '--count_flag',
                                                               '--help', '--normal_flag', '--remainder_flag']),

    # Non-default flag prefix character (+)
    ('plus_flag', '+', ['++help', '++normal_flag', '+h', '+n']),
    ('plus_flag', '++', ['++help', '++normal_flag']),

    # Flag completion should not occur after '--' since that tells argparse all remaining arguments are non-flags
    ('flag --', '--', []),
    ('flag --help --', '--', []),
    ('plus_flag --', '++', []),
    ('plus_flag ++help --', '++', [])
])
def test_autcomp_flag_completion(ac_app, command_and_args, text, completions):
    line = '{} {}'.format(command_and_args, text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


@pytest.mark.parametrize('flag, text, completions', [
    ('-l', '', static_choices_list),
    ('--list', 's', ['static', 'stop']),
    ('-f', '', choices_from_function),
    ('--function', 'ch', ['choices', 'chatty']),
    ('-m', '', choices_from_method),
    ('--method', 'm', ['method', 'most']),
    ('-i', '', static_int_choices_list),
    ('--int', '1', ['1 ']),
    ('--int', '-', [-1, -2, -12]),
    ('--int', '-1', [-1, -12])
])
def test_autocomp_flag_choices_completion(ac_app, flag, text, completions):
    import numbers

    line = 'choices {} {}'.format(flag, text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    # Numbers will be sorted in ascending order and then converted to strings by AutoCompleter
    if all(isinstance(x, numbers.Number) for x in completions):
        completions.sort()
        completions = [str(x) for x in completions]
    else:
        completions.sort(key=ac_app.default_sort_key)

    assert ac_app.completion_matches == completions


@pytest.mark.parametrize('pos, text, completions', [
    (1, '', static_choices_list),
    (1, 's', ['static', 'stop']),
    (2, '', choices_from_function),
    (2, 'ch', ['choices', 'chatty']),
    (3, '', choices_from_method),
    (3, 'm', ['method', 'most'])
])
def test_autocomp_positional_choices_completion(ac_app, pos, text, completions):
    # Generate line were preceding positionals are already filled
    line = 'choices {} {}'.format('foo ' * (pos - 1), text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


@pytest.mark.parametrize('flag, text, completions', [
    ('-f', '', completions_from_function),
    ('--function', 'f', ['function', 'fairly']),
    ('-m', '', completions_from_method),
    ('--method', 'm', ['method', 'missed'])
])
def test_autocomp_flag_completers(ac_app, flag, text, completions):
    line = 'completer {} {}'.format(flag, text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


@pytest.mark.parametrize('pos, text, completions', [
    (1, '', completions_from_function),
    (1, 'c', ['completions', 'complete']),
    (2, '', completions_from_method),
    (2, 'm', ['method', 'missed'])
])
def test_autocomp_positional_completers(ac_app, pos, text, completions):
    # Generate line were preceding positionals are already filled
    line = 'completer {} {}'.format('foo ' * (pos - 1), text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


def test_autocomp_blank_token(ac_app):
    """Force a blank token to make sure AutoCompleter consumes them like argparse does"""
    from cmd2.argparse_completer import AutoCompleter

    blank = ''

    # Blank flag arg
    text = ''
    line = 'completer -m {} {}'.format(blank, text)
    endidx = len(line)
    begidx = endidx - len(text)

    completer = AutoCompleter(ac_app.completer_parser, ac_app)
    tokens = ['completer', '-f', blank, text]
    completions = completer.complete_command(tokens, text, line, begidx, endidx)
    assert completions == completions_from_function

    # Blank positional arg
    text = ''
    line = 'completer {} {}'.format(blank, text)
    endidx = len(line)
    begidx = endidx - len(text)

    completer = AutoCompleter(ac_app.completer_parser, ac_app)
    tokens = ['completer', blank, text]
    completions = completer.complete_command(tokens, text, line, begidx, endidx)
    assert completions == completions_from_method


@pytest.mark.parametrize('num_aliases, show_description', [
    # The number of completion results determines if the description field of CompletionItems gets displayed
    # in the tab completions. The count must be greater than 1 and less than ac_app.max_completion_items,
    # which defaults to 50.
    (1, False),
    (5, True),
    (100, False)
])
def test_completion_items(ac_app, num_aliases, show_description):
    # Create aliases
    for i in range(0, num_aliases):
        run_cmd(ac_app, 'alias create fake{} help'.format(i))

    assert len(ac_app.aliases) == num_aliases

    text = 'fake'
    line = 'alias list {}'.format(text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    assert first_match is not None
    assert len(ac_app.completion_matches) == num_aliases
    assert len(ac_app.display_matches) == num_aliases

    # If show_description is True, the alias's value will be in the display text
    assert ('help' in ac_app.display_matches[0]) == show_description


@pytest.mark.parametrize('args, completions', [
    # Flag with nargs = 2
    ('--set_value', set_value_choices),
    ('--set_value set', ['value', 'choices']),

    # Both args are filled. At positional arg now.
    ('--set_value set value', positional_choices),

    # Using the flag again will reset the choices available
    ('--set_value set value --set_value', set_value_choices),

    # Flag with nargs = ONE_OR_MORE
    ('--one_or_more', one_or_more_choices),
    ('--one_or_more one', ['or', 'more', 'choices']),

    # Flag with nargs = OPTIONAL
    ('--optional', optional_choices),

    # Only one arg allowed for an OPTIONAL. At positional now.
    ('--optional optional', positional_choices),

    # Flag with nargs range (1, 2)
    ('--range', range_choices),
    ('--range some', ['range', 'choices']),

    # Already used 2 args so at positional
    ('--range some range', positional_choices),

    # Flag with nargs = REMAINDER
    ('--remainder', remainder_choices),
    ('--remainder remainder ', ['choices ']),

    # No more flags can appear after a REMAINDER flag)
    ('--remainder choices --set_value', ['remainder ']),

    # Double dash ends the current flag
    ('--range choice --', positional_choices),

    # Double dash ends a REMAINDER flag
    ('--remainder remainder --', positional_choices),

    # No more flags after a double dash
    ('-- --one_or_more ', positional_choices),

    # Consume positional
    ('', positional_choices),
    ('positional', ['the', 'choices']),

    # Intermixed flag and positional
    ('positional --set_value', set_value_choices),
    ('positional --set_value set', ['choices', 'value']),

    # Intermixed flag and positional with flag finishing
    ('positional --set_value set value', ['the', 'choices']),
    ('positional --range choice --', ['the', 'choices']),

    # REMAINDER positional
    ('the positional', remainder_choices),
    ('the positional remainder', ['choices ']),
    ('the positional remainder choices', []),

    # REMAINDER positional. Flags don't work in REMAINDER
    ('the positional --set_value', remainder_choices),
    ('the positional remainder --set_value', ['choices '])
])
def test_autcomp_nargs(ac_app, args, completions):
    text = ''
    line = 'nargs {} {}'.format(args, text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    if completions:
        assert first_match is not None
    else:
        assert first_match is None

    assert ac_app.completion_matches == sorted(completions, key=ac_app.default_sort_key)


@pytest.mark.parametrize('command_and_args, text, is_error', [
    # Flag is finished before moving on
    ('hint --flag foo --', '', False),
    ('hint --flag foo --help', '', False),
    ('hint --flag foo', '--', False),

    ('nargs --one_or_more one --', '', False),
    ('nargs --one_or_more one or --set_value', '', False),
    ('nargs --one_or_more one or more', '--', False),

    ('nargs --set_value set value --', '', False),
    ('nargs --set_value set value --one_or_more', '', False),
    ('nargs --set_value set value', '--', False),
    ('nargs --set_val set value', '--', False),  # This exercises our abbreviated flag detection

    ('nargs --range choices --', '', False),
    ('nargs --range choices range --set_value', '', False),
    ('nargs --range range', '--', False),

    # Flag is not finished before moving on
    ('hint --flag --', '', True),
    ('hint --flag --help', '', True),
    ('hint --flag', '--', True),

    ('nargs --one_or_more --', '', True),
    ('nargs --one_or_more --set_value', '', True),
    ('nargs --one_or_more', '--', True),

    ('nargs --set_value set --', '', True),
    ('nargs --set_value set --one_or_more', '', True),
    ('nargs --set_value set', '--', True),
    ('nargs --set_val set', '--', True),  # This exercises our abbreviated flag detection

    ('nargs --range --', '', True),
    ('nargs --range --set_value', '', True),
    ('nargs --range', '--', True),
])
def test_unfinished_flag_error(ac_app, command_and_args, text, is_error, capsys):
    line = '{} {}'.format(command_and_args, text)
    endidx = len(line)
    begidx = endidx - len(text)

    complete_tester(text, line, begidx, endidx, ac_app)

    out, err = capsys.readouterr()
    assert is_error == all(x in out for x in ["Error:\n", "expected"])


def test_completion_items_default_header(ac_app):
    from cmd2.argparse_completer import DEFAULT_DESCRIPTIVE_HEADER

    text = ''
    line = 'choices -n {}'.format(text)
    endidx = len(line)
    begidx = endidx - len(text)

    # This positional argument did not provide a descriptive header, so it should be DEFAULT_DESCRIPTIVE_HEADER
    complete_tester(text, line, begidx, endidx, ac_app)
    assert DEFAULT_DESCRIPTIVE_HEADER in ac_app.completion_header


@pytest.mark.parametrize('command_and_args, text, has_hint', [
    # Normal cases
    ('hint', '', True),
    ('hint --flag', '', True),
    ('hint --suppressed_help', '', False),
    ('hint --suppressed_hint', '--', False),

    # Hint because flag does not have enough values to be considered finished
    ('nargs --one_or_more', '-', True),

    # This flag has reached its minimum value count and therefore a new flag could start.
    # However the flag can still consume values and the text is not a single prefix character.
    # Therefor a hint will be shown.
    ('nargs --one_or_more choices', 'bad_completion', True),

    # Like the previous case, but this time text is a single prefix character which will cause flag
    # name completion to occur instead of a hint for the current flag.
    ('nargs --one_or_more choices', '-', False),

    # Hint because this is a REMAINDER flag and therefore no more flag name completions occur.
    ('nargs --remainder', '-', True),

    # No hint for the positional because text is a single prefix character which results in flag name completion
    ('hint', '-', False),

    # Hint because this is a REMAINDER positional and therefore no more flag name completions occur.
    ('nargs the choices', '-', True),
    ('nargs the choices remainder', '-', True),
])
def test_autocomp_hint(ac_app, command_and_args, text, has_hint, capsys):
    line = '{} {}'.format(command_and_args, text)
    endidx = len(line)
    begidx = endidx - len(text)

    complete_tester(text, line, begidx, endidx, ac_app)
    out, err = capsys.readouterr()
    assert has_hint == ("Hint:\n" in out)


def test_autocomp_hint_multiple_lines(ac_app, capsys):
    text = ''
    line = 'hint {}'.format(text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    out, err = capsys.readouterr()

    assert first_match is None
    assert out == '''
Hint:
  HINT_POS                here is a hint
                          with new lines

'''


def test_autocomp_hint_no_help_text(ac_app, capsys):
    text = ''
    line = 'hint foo {}'.format(text)
    endidx = len(line)
    begidx = endidx - len(text)

    first_match = complete_tester(text, line, begidx, endidx, ac_app)
    out, err = capsys.readouterr()

    assert first_match is None
    assert not out == '''
Hint:
  NO_HELP_POS            

'''


def test_single_prefix_char():
    from cmd2.argparse_completer import _single_prefix_char
    parser = Cmd2ArgumentParser(prefix_chars='-+')

    # Invalid
    assert not _single_prefix_char('', parser)
    assert not _single_prefix_char('--', parser)
    assert not _single_prefix_char('-+', parser)
    assert not _single_prefix_char('++has space', parser)
    assert not _single_prefix_char('foo', parser)

    # Valid
    assert _single_prefix_char('-', parser)
    assert _single_prefix_char('+', parser)


def test_looks_like_flag():
    from cmd2.argparse_completer import _looks_like_flag
    parser = Cmd2ArgumentParser()

    # Does not start like a flag
    assert not _looks_like_flag('', parser)
    assert not _looks_like_flag('non-flag', parser)
    assert not _looks_like_flag('-', parser)
    assert not _looks_like_flag('--has space', parser)
    assert not _looks_like_flag('-2', parser)

    # Does start like a flag
    assert _looks_like_flag('--', parser)
    assert _looks_like_flag('-flag', parser)
    assert _looks_like_flag('--flag', parser)


def test_complete_command_no_tokens(ac_app):
    from cmd2.argparse_completer import AutoCompleter

    parser = Cmd2ArgumentParser()
    ac = AutoCompleter(parser, ac_app)

    completions = ac.complete_command(tokens=[], text='', line='', begidx=0, endidx=0)
    assert not completions


def test_complete_command_help_no_tokens(ac_app):
    from cmd2.argparse_completer import AutoCompleter

    parser = Cmd2ArgumentParser()
    ac = AutoCompleter(parser, ac_app)

    completions = ac.complete_command_help(tokens=[], text='', line='', begidx=0, endidx=0)
    assert not completions
