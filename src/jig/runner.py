import json
import sys
from datetime import datetime

from git import Repo

from jig.exc import GitRepoNotInitialized
from jig.conf import PLUGIN_CHECK_FOR_UPDATES
from jig.gitutils.checks import repo_jiginitialized
from jig.gitutils.branches import parse_rev_range, prepare_working_directory
from jig.diffconvert import GitDiffIndex
from jig.plugins import get_jigconfig, PluginManager
from jig.plugins.tools import (
    set_jigconfig, last_checked_for_updates, plugins_have_updates,
    set_checked_for_updates, update_plugins)
from jig.commands import get_command, list_commands
from jig.output import ConsoleView, ResultsCollator
from jig.formatters.fancy import FancyFormatter

try:
    from collections import OrderedDict
except ImportError:   # pragma: no cover
    from ordereddict import OrderedDict


def _diff_for(gitrepo, rev_range=None):
    """
    Get a list of :py:class:`git.diff.Diff` objects for the repository.

    :param git.repo.base.Repo gitrepo: Git repository
    :param RevRangePair rev_range: optional revision to use instead of the
        Git index
    """
    if rev_range:
        return rev_range.a.diff(rev_range.b)
    else:
        # Assume we want a diff between what is staged and HEAD
        try:
            return gitrepo.head.commit.diff()
        except ValueError:
            return None


class Runner(object):

    """
    Runs jig in a Git repo.

    """
    def __init__(self, view=None, formatter=None):
        self.view = view or ConsoleView()
        create_formatter = lambda f: f() if f else FancyFormatter()
        self.formatter = create_formatter(formatter)

    def fromhook(self, gitrepo):
        """
        Main entry point called from pre-commit hook.

        :param unicode gitrepo: path to the Git repository
        """
        return self.main(gitrepo)

    def main(self, gitrepo, plugin=None, rev_range=None, interactive=True):
        """
        Run Jig on the given Git repository.

        :param unicode gitrepo: path to the Git repository
        :param unicode plugin: the name of the plugin to run, if None then run
            all plugins
        :param unicode rev_range: the revision range to use instead of the Git
            index
        :param bool interactive: if True then the user will be prompted to
            commit or cancel when any messages are generated by the plugins.
        """
        sys.stdin = open('/dev/tty')

        if interactive:
            # Check to see if the plugins need updating
            now = datetime.utcnow()

            with self.view.out():
                last_checked = last_checked_for_updates(gitrepo) or \
                    datetime.fromtimestamp(0)

            if now > last_checked + PLUGIN_CHECK_FOR_UPDATES:
                self.update_plugins(gitrepo)

        with self.view.out() as printer:
            if not repo_jiginitialized(gitrepo):
                raise GitRepoNotInitialized(
                    'This repository has not been initialized.')

            if rev_range:
                rev_range_parsed = parse_rev_range(gitrepo, rev_range)
            else:
                rev_range_parsed = None

            with prepare_working_directory(gitrepo, rev_range_parsed):
                results = self.results(   # pragma: no branch
                    gitrepo,
                    plugin=plugin,
                    rev_range=rev_range_parsed
                )

            if not results:
                report_counts = (0, 0, 0)
            else:
                collator = ResultsCollator(results)

                report_counts = self.formatter.print_results(printer, collator)

        if interactive and report_counts and sum(report_counts):
            # Git will run a pre-commit hook with stdin pointed at /dev/null.
            # We will reconnect to the tty so that raw_input works.
            while True:
                try:
                    answer = raw_input(
                        '\nCommit anyway (hit "c"), or stop (hit "s"): ')
                except KeyboardInterrupt:
                    sys.exit(1)
                if answer and answer[0].lower() == 's':
                    sys.exit(1)
                elif answer and answer[0].lower() == 'c':
                    break

        sys.exit(0)

    def fromconsole(self, argv):
        """
        Console entry point for the jig script.

        Where ``argv`` is ``sys.argv``.
        """
        # Quick copy
        argv = argv[:]
        # Our script is the first element
        argv.pop(0)

        try:
            # Next argument is the command
            command = get_command(argv.pop(0))
            command(argv)   # pragma: no cover
        except (ImportError, IndexError):
            # If it's empty
            self.view.print_help(list_commands())

    def update_plugins(self, gitrepo):
        """
        Prompt the user to update the plugins if available.

        :params string gitrepo: path to the Git repository
        """
        with self.view.out() as printer:
            printer(u'Checking for plugin updates\u2026')

        # Examine the remotes of the installed plugins
        if not plugins_have_updates(gitrepo):
            # If we don't have updates, change the date forward for the next
            # check to be a few days from now.
            set_jigconfig(gitrepo, set_checked_for_updates(gitrepo))
            # No updates, so nothing else we need to do
            return False

        # We have updates, ask the user if they want to fetch from the
        # remote and install them
        while True:
            try:
                answer = raw_input(
                    '\nPlugin updates are available, install ("y"/"n"): ')
            except KeyboardInterrupt:
                # If the user CTRL-C's out, leave the last date checked for
                # updates alone. Their intention with this is not really a
                # yes or a now so play it safe.
                return False
            else:
                # No KeyboardInterrupt, this is good enough to go ahead and
                # move the date we checked for updates to "now".
                set_jigconfig(gitrepo, set_checked_for_updates(gitrepo))
                # We now have a possible answer, do the appropriate thing
                if answer and answer[0].lower() == 'y':
                    update_plugins(gitrepo)
                    return True
                if answer and answer[0].lower() == 'n':
                    return False

    def results(self, gitrepo, plugin=None, rev_range=None):
        """
        Run jig in the repository and return results.

        Results will be a dictionary where the keys will be individual plugins
        and the value the result of calling their ``pre_commit()`` methods.

        :param unicode gitrepo: path to the Git repository
        :param unicode plugin: the name of the plugin to run, if None then run
            all plugins
        :param RevRangePair rev_range: the revision range to use instead of the
            Git index
        """
        pm = PluginManager(get_jigconfig(gitrepo))

        # Check to make sure we have some plugins to run
        with self.view.out() as printer:
            if len(pm.plugins) == 0:
                printer(
                    'There are no plugins installed, '
                    'use jig install to add some.')
                return

            self.repo = Repo(gitrepo)

            diff = _diff_for(self.repo, rev_range)

            if diff is None:
                # No diff on head, no commits have been written yet
                printer(
                    'This repository is empty, jig needs at '
                    'least 1 commit to continue.')
                # Let execution continue so they *can* commit that first
                # changeset. This is a special mode that should not cause Jig
                # to exit with non-zero.
                return

            if len(diff) == 0:
                # There is nothing changed in this repository, no need for
                # jig to run so we exit with 0.
                printer(
                    'No staged changes in the repository, skipping jig.')
                return

        # Our git diff index is an object that makes working with the diff much
        # easier in the context of our plugins.
        gdi = GitDiffIndex(gitrepo, diff)

        # Go through the plugins and gather up the results
        results = OrderedDict()
        for installed in pm.plugins:
            if plugin and installed.name != plugin:
                # This plugin doesn't match the requested
                continue

            retcode, stdout, stderr = installed.pre_commit(gdi)

            try:
                # Is it JSON data?
                data = json.loads(stdout)
            except ValueError:
                # Not JSON
                data = stdout

            results[installed] = (retcode, data, stderr)

        return results
