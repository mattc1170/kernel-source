#!/usr/bin/python
# -*- coding: utf-8 -*-

from __future__ import print_function

import collections
import os
import os.path
import pygit2
import signal
import subprocess
import sys

import lib_tag

import git_sort


class KSException(BaseException):
    pass


class KSError(KSException):
    pass


# http://stackoverflow.com/questions/22077881/yes-reporting-error-with-subprocess-communicate
def restore_signals(): # from http://hg.python.org/cpython/rev/768722b2ae0a/
    signals = ('SIGPIPE', 'SIGXFZ', 'SIGXFSZ')
    for sig in signals:
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), signal.SIG_DFL)


def check_series():
    def check():
        return (open("series").readline().strip() ==
                "# Kernel patches configuration file")

    try:
        retval = check()
    except IOError as err:
        print("Error: could not read series file: %s" % (err,), file=sys.stderr)
        return False

    if retval:
        return True
    
    subprocess.call(["quilt", "top"], preexec_fn=restore_signals)
    if check():
        return True
    else:
        print("Error: series file does not look like series.conf. "
              "Make sure you are using the modified `quilt`; see "
              "scripts/git_sort/README.md.", file=sys.stderr)
        return False


def firstword(value):
    return value.split(None, 1)[0]


def libdir():
    return os.path.dirname(os.path.realpath(__file__))


class KSNotFound(KSException):
    pass


def split_series(series):
    before = []
    inside = []
    after = []

    whitespace = []
    comments = []

    current = before
    for line in series:
        l = line.strip()

        if l == "":
            if comments:
                current.extend(comments)
                comments = []
            whitespace.append(line)
            continue
        elif l.startswith("#"):
            if whitespace:
                current.extend(whitespace)
                whitespace = []
            comments.append(line)

            if current == before and l.lower() == "# sorted patches":
                current = inside
            elif current == inside and l.lower() == "# end of sorted patches":
                current = after
        else:
            if comments:
                current.extend(comments)
                comments = []
            if whitespace:
                current.extend(whitespace)
                whitespace = []
            current.append(line)

    if current == before:
        raise KSNotFound("Sorted subseries not found.")

    current.extend(comments)
    current.extend(whitespace)

    return (before, inside, after,)


def filter_patches(line):
    line = line.strip()

    if line == "" or line.startswith(("#", "-", "+",)):
        return False
    else:
        return True


def series_header(series):
    header = []

    for line in series:
        if not filter_patches(line):
            header.append(line)
            continue
        else:
            break

    return header


def series_footer(series):
    return series_header(reversed(series))


def filter_sorted(series):
    """
    Return upstream patch names from the sorted section
    """
    result = []

    for line in series:
        line = line.strip()
        if line == "# out-of-tree patches":
            break

        if line == "" or line.startswith(("#", "-", "+",)):
            continue

        result.append(line)

    return result


def repo_path():
    try:
        search_path = subprocess.check_output(
            os.path.join(libdir(), "..", "linux_git.sh"),
            preexec_fn=restore_signals).strip()
    except subprocess.CalledProcessError:
        print("Error: Could not determine mainline linux git repository path.",
              file=sys.stderr)
        sys.exit(1)
    return pygit2.discover_repository(search_path)


# http://stackoverflow.com/questions/1158076/implement-touch-using-python
def touch(fname, times=None):
    with open(fname, 'a'):
        os.utime(fname, times)


def find_commit_in_series(commit, series):
    for patch in [firstword(l) for l in series if filter_patches(l)]:
        path = os.path.join("patches", patch)
        f = open(path)
        if commit in [firstword(t) for t in lib_tag.tag_get(f, "Git-commit")]:
            return f


# https://stackoverflow.com/a/952952
flatten = lambda l: [item for sublist in l for item in sublist]


def sequence_insert(series, rev, top):
    """
    top is the top applied patch, None if none are applied.

    Caller must chdir to where the entries in series can be found.

    Returns the name of the new top patch and how many must be applied/popped.
    """
    git_dir = repo_path()
    if "GIT_DIR" not in os.environ:
        # this is for the `git log` call in git_sort.py
        os.environ["GIT_DIR"] = git_dir
    repo = pygit2.Repository(git_dir)
    try:
        commit = str(repo.revparse_single(rev).id)
    except ValueError:
        raise KSError("\"%s\" is not a valid revision." % (rev,))
    except KeyError:
        raise KSError("Revision \"%s\" not found in \"%s\"." % (
            rev, git_dir,))

    before, inside, after = [
        [firstword(line) for line in lines if filter_patches(line)]
        for lines in split_series(series)]
    current_patches = flatten([before, inside, after])

    if top is None:
        top_index = 0
    else:
        top_index = current_patches.index(top) + 1

    input_entries = []
    for patch in inside:
        entry = InputEntry(patch)
        entry.from_patch(repo, patch)
        input_entries.append(entry)

    marker = "# new commit"
    entry = InputEntry(marker)
    entry.commit = commit
    input_entries.append(entry)

    sorted_entries = series_sort(repo, input_entries)
    for head, patches in sorted_entries:
        if head.rev == "unknown/local patches":
            if patches[0] == marker:
                msg = "New commit %s" % commit
            else:
                f = open(patches[0])
                commit_tags = lib_tag.tag_get(f, "Git-commit")
                rev = firstword(commit_tags[0])
                msg = "Commit %s first found in patch \"%s\"" % (rev,
                    patches[0],)
            raise KSError(msg + " appears to be from a repository which is "
                          "not indexed. Please edit \"remotes\" in git_sort.py "
                          "and submit a patch.")
    sorted_patches = flatten([
        before,
        [patch
         for head, patches in sorted_entries
         for patch in patches],
        after])
    commit_pos = sorted_patches.index("# new commit")
    if commit_pos == 0:
        # should be inserted first in series
        name = ""
    else:
        name = sorted_patches[commit_pos - 1]
    del sorted_patches[commit_pos]

    if sorted_patches != current_patches:
        raise KSError("Subseries is not sorted. "
                      "Please run scripts/series_sort.py.")

    return (name, commit_pos - top_index,)


class InputEntry(object):
    def __init__(self, value):
        self.commit = None
        self.subsys = None
        self.oot = False

        self.value = value

    def from_patch(self, repo, patch):
        if not os.path.exists(patch):
            raise KSError("Could not find patch \"%s\"" % (patch,))

        f = open(patch)
        commit_tags = lib_tag.tag_get(f, "Git-commit")
        if not commit_tags:
            self.oot = True
            return

        rev = firstword(commit_tags[0])
        try:
            commit = repo.revparse_single(rev)
        except ValueError:
            raise KSError("Git-commit tag \"%s\" in patch \"%s\" is not a valid revision." %
                              (rev, patch,))
        except KeyError:
            repo_tags = lib_tag.tag_get(f, "Git-repo")
            if not repo_tags:
                raise KSError(
                    "Commit \"%s\" not found and no Git-repo specified. "
                    "Either the repository at \"%s\" is outdated or patch \"%s\" is tagged improperly." % (
                        rev, repo.path, patch,))
            elif len(repo_tags) > 1:
                raise KSError("Multiple Git-repo tags found. "
                              "Patch \"%s\" is tagged improperly." %
                              (patch,))
            self.subsys = git_sort.RepoURL(repo_tags[0])
        else:
            self.commit = str(commit.id)


def series_sort(repo, entries):
    """
    entries is a list of InputEntry objects

    Returns a list of
        (Head, [series.conf line with a patch name],)

    head name may be a "virtual head" like "out-of-tree patches".
    """
    tagged = collections.defaultdict(list)
    for input_entry in entries:
        if input_entry.commit:
            tagged[input_entry.commit].append(input_entry.value)

    subsys = collections.defaultdict(list)
    index = git_sort.SortIndex(repo)
    for sorted_entry in index.sort(tagged):
        subsys[sorted_entry.head].extend(sorted_entry.value)

    url_map = get_url_map()
    for e in entries:
        if e.subsys:
            try:
                head = url_map[e.subsys]
            except KeyError:
                patch = firstword(e.value)
                f = open(patch)
                commit_tags = lib_tag.tag_get(f, "Git-commit")
                rev = firstword(commit_tags[0])
                raise KSError(
                    "Commit %s first found in patch \"%s\" appears to be from "
                    "a repository which is not indexed. Please edit "
                    "\"remotes\" in git_sort.py and submit a patch." % (
                        rev, patch,))
            subsys[head].append(e.value)

    result = []
    for head in git_sort.remotes:
        if head in subsys:
            result.append((head, subsys[head],))
            del subsys[head]

    if tagged:
        result.append((
            git_sort.Head(git_sort.RepoURL(str(None)), "unknown/local patches"),
            [value for value_list in tagged.values() for value in value_list],))

    result.append((
        git_sort.Head(git_sort.RepoURL(str(None)), "out-of-tree patches"),
        [e.value for e in entries if e.oot],))

    return result


def get_url_map():
    result = {}
    for head in git_sort.remotes:
        if head.repo_url in result:
            raise KSException("URL mapping is ambiguous, \"%s\" may map to "
                              "multiple head names" % (str(head.repo_url),))
        result[head.repo_url] = head
    return result


def series_format(entries):
    """
    entries is a list of
        (Head, [series.conf line with a patch name],)
    """
    result = []

    for head, lines in entries:
        if head != git_sort.remotes[0]:
            result.extend(["\n", "\t# %s\n" % (str(head),)])
        result.extend(lines)

    return result
