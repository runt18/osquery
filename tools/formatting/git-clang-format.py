#!/usr/bin/env python
#
#===- git-clang-format - ClangFormat Git Integration ---------*- python -*--===#
#
#                     The LLVM Compiler Infrastructure
#
# This file is distributed under the University of Illinois Open Source
# License. See LLVM-LICENSE.TXT for details.
#
#===------------------------------------------------------------------------===#

r"""
clang-format git integration
============================

This file provides a clang-format integration for git. Put it somewhere in your
path and ensure that it is executable. Then, "git clang-format" will invoke
clang-format on the changes in current files or a specific commit.

For further details, run:
git clang-format -h

Requires Python 2.7
"""

import argparse
import collections
import contextlib
import errno
import os
import re
import subprocess
import sys

usage = 'git clang-format [OPTIONS] [<commit>] [--] [<file>...]'

desc = '''
Run clang-format on all lines that differ between the working directory
and <commit>, which defaults to HEAD.  Changes are only applied to the working
directory.

The following git-config settings set the default of the corresponding option:
  clangFormat.binary
  clangFormat.commit
  clangFormat.extension
  clangFormat.style
'''

# Name of the temporary index file in which save the output of clang-format.
# This file is created within the .git directory.
temp_index_basename = 'clang-format-index'


Range = collections.namedtuple('Range', 'start, count')


def main():
  config = load_git_config()

  # In order to keep '--' yet allow options after positionals, we need to
  # check for '--' ourselves.  (Setting nargs='*' throws away the '--', while
  # nargs=argparse.REMAINDER disallows options after positionals.)
  argv = sys.argv[1:]
  try:
    idx = argv.index('--')
  except ValueError:
    dash_dash = []
  else:
    dash_dash = argv[idx:]
    argv = argv[:idx]

  default_extensions = ','.join([
      # From clang/lib/Frontend/FrontendOptions.cpp, all lower case
      'c', 'h',  # C
      'm',  # ObjC
      'mm',  # ObjC++
      'cc', 'cp', 'cpp', 'c++', 'cxx', 'hpp',  # C++
      # Other languages that clang-format supports
      'proto', 'protodevel',  # Protocol Buffers
      'js',  # JavaScript
      ])

  p = argparse.ArgumentParser(
    usage=usage, formatter_class=argparse.RawDescriptionHelpFormatter,
    description=desc)
  p.add_argument('--binary',
                 default=config.get('clangformat.binary', 'clang-format'),
                 help='path to clang-format'),
  p.add_argument('--commit',
                 default=config.get('clangformat.commit', 'HEAD'),
                 help='default commit to use if none is specified'),
  p.add_argument('--diff', action='store_true',
                 help='print a diff instead of applying the changes')
  p.add_argument('--extensions',
                 default=config.get('clangformat.extensions',
                                    default_extensions),
                 help=('comma-separated list of file extensions to format, '
                       'excluding the period and case-insensitive')),
  p.add_argument('-f', '--force', action='store_true',
                 help='allow changes to unstaged files')
  p.add_argument('-p', '--patch', action='store_true',
                 help='select hunks interactively')
  p.add_argument('-q', '--quiet', action='count', default=0,
                 help='print less information')
  p.add_argument('--style',
                 default=config.get('clangformat.style', None),
                 help='passed to clang-format'),
  p.add_argument('-v', '--verbose', action='count', default=0,
                 help='print extra information')
  # We gather all the remaining positional arguments into 'args' since we need
  # to use some heuristics to determine whether or not <commit> was present.
  # However, to print pretty messages, we make use of metavar and help.
  p.add_argument('args', nargs='*', metavar='<commit>',
                 help='revision from which to compute the diff')
  p.add_argument('ignored', nargs='*', metavar='<file>...',
                 help='if specified, only consider differences in these files')
  opts = p.parse_args(argv)

  opts.verbose -= opts.quiet
  del opts.quiet

  commit, files = interpret_args(opts.args, dash_dash, opts.commit)
  changed_lines = compute_diff_and_extract_lines(commit, files)
  if opts.verbose >= 1:
    ignored_files = set(changed_lines)
  filter_by_extension(changed_lines, opts.extensions.lower().split(','))
  if opts.verbose >= 1:
    ignored_files.difference_update(changed_lines)
    if ignored_files:
      print 'Ignoring changes in the following files (wrong extension):'
      for filename in ignored_files:
        print '   ', filename
    if changed_lines:
      print 'Running clang-format on the following files:'
      for filename in changed_lines:
        print '   ', filename
  if not changed_lines:
    print 'no modified files to format'
    return
  # The computed diff outputs absolute paths, so we must cd before accessing
  # those files.
  cd_to_toplevel()
  old_tree = create_tree_from_workdir(changed_lines)
  new_tree = run_clang_format_and_save_to_tree(changed_lines,
                                               binary=opts.binary,
                                               style=opts.style)
  if opts.verbose >= 1:
    print 'old tree:', old_tree
    print 'new tree:', new_tree
  if old_tree == new_tree:
    if opts.verbose >= 0:
      print 'clang-format did not modify any files'
  elif opts.diff:
    print_diff(old_tree, new_tree)
  else:
    changed_files = apply_changes(old_tree, new_tree, force=opts.force,
                                  patch_mode=opts.patch)
    if (opts.verbose >= 0 and not opts.patch) or opts.verbose >= 1:
      print 'changed files:'
      for filename in changed_files:
        print '   ', filename


def load_git_config(non_string_options=None):
  """Return the git configuration as a dictionary.

  All options are assumed to be strings unless in `non_string_options`, in which
  is a dictionary mapping option name (in lower case) to either "--bool" or
  "--int"."""
  if non_string_options is None:
    non_string_options = {}
  out = {}
  for entry in run('git', 'config', '--list', '--null').split('\0'):
    if entry:
      name, value = entry.split('\n', 1)
      if name in non_string_options:
        value = run('git', 'config', non_string_options[name], name)
      out[name] = value
  return out


def interpret_args(args, dash_dash, default_commit):
  """Interpret `args` as "[commit] [--] [files...]" and return (commit, files).

  It is assumed that "--" and everything that follows has been removed from
  args and placed in `dash_dash`.

  If "--" is present (i.e., `dash_dash` is non-empty), the argument to its
  left (if present) is taken as commit.  Otherwise, the first argument is
  checked if it is a commit or a file.  If commit is not given,
  `default_commit` is used."""
  if dash_dash:
    if len(args) == 0:
      commit = default_commit
    elif len(args) > 1:
      die('at most one commit allowed; {0:d} given'.format(len(args)))
    else:
      commit = args[0]
    object_type = get_object_type(commit)
    if object_type not in ('commit', 'tag'):
      if object_type is None:
        die("'{0!s}' is not a commit".format(commit))
      else:
        die("'{0!s}' is a {1!s}, but a commit was expected".format(commit, object_type))
    files = dash_dash[1:]
  elif args:
    if disambiguate_revision(args[0]):
      commit = args[0]
      files = args[1:]
    else:
      commit = default_commit
      files = args
  else:
    commit = default_commit
    files = []
  return commit, files


def disambiguate_revision(value):
  """Returns True if `value` is a revision, False if it is a file, or dies."""
  # If `value` is ambiguous (neither a commit nor a file), the following
  # command will die with an appropriate error message.
  run('git', 'rev-parse', value, verbose=False)
  object_type = get_object_type(value)
  if object_type is None:
    return False
  if object_type in ('commit', 'tag'):
    return True
  die('`{0!s}` is a {1!s}, but a commit or filename was expected'.format(value, object_type))


def get_object_type(value):
  """Returns a string description of an object's type, or None if it is not
  a valid git object."""
  cmd = ['git', 'cat-file', '-t', value]
  p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  stdout, stderr = p.communicate()
  if p.returncode != 0:
    return None
  return stdout.strip()


def compute_diff_and_extract_lines(commit, files):
  """Calls compute_diff() followed by extract_lines()."""
  diff_process = compute_diff(commit, files)
  changed_lines = extract_lines(diff_process.stdout)
  diff_process.stdout.close()
  diff_process.wait()
  if diff_process.returncode != 0:
    # Assume error was already printed to stderr.
    sys.exit(2)
  return changed_lines


def compute_diff(commit, files):
  """Return a subprocess object producing the diff from `commit`.

  The return value's `stdin` file object will produce a patch with the
  differences between the working directory and `commit`, filtered on `files`
  (if non-empty).  Zero context lines are used in the patch."""
  cmd = ['git', 'diff-index', '-p', '-U0', commit, '--']
  cmd.extend(files)
  p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
  p.stdin.close()
  return p


def extract_lines(patch_file):
  """Extract the changed lines in `patch_file`.

  The return value is a dictionary mapping filename to a list of (start_line,
  line_count) pairs.

  The input must have been produced with ``-U0``, meaning unidiff format with
  zero lines of context.  The return value is a dict mapping filename to a
  list of line `Range`s."""
  matches = {}
  for line in patch_file:
    match = re.search(r'^\+\+\+\ [^/]+/(.*)', line)
    if match:
      filename = match.group(1).rstrip('\r\n')
    match = re.search(r'^@@ -[0-9,]+ \+(\d+)(,(\d+))?', line)
    if match:
      start_line = int(match.group(1))
      line_count = 1
      if match.group(3):
        line_count = int(match.group(3))
      if line_count > 0:
        matches.setdefault(filename, []).append(Range(start_line, line_count))
  return matches


def filter_by_extension(dictionary, allowed_extensions):
  """Delete every key in `dictionary` that doesn't have an allowed extension.

  `allowed_extensions` must be a collection of lowercase file extensions,
  excluding the period."""
  allowed_extensions = frozenset(allowed_extensions)
  for filename in dictionary.keys():
    base_ext = filename.rsplit('.', 1)
    if len(base_ext) == 1 or base_ext[1].lower() not in allowed_extensions:
      del dictionary[filename]


def cd_to_toplevel():
  """Change to the top level of the git repository."""
  toplevel = run('git', 'rev-parse', '--show-toplevel')
  os.chdir(toplevel)


def create_tree_from_workdir(filenames):
  """Create a new git tree with the given files from the working directory.

  Returns the object ID (SHA-1) of the created tree."""
  return create_tree(filenames, '--stdin')


def run_clang_format_and_save_to_tree(changed_lines, binary='clang-format',
                                      style=None):
  """Run clang-format on each file and save the result to a git tree.

  Returns the object ID (SHA-1) of the created tree."""
  def index_info_generator():
    for filename, line_ranges in changed_lines.iteritems():
      mode = oct(os.stat(filename).st_mode)
      blob_id = clang_format_to_blob(filename, line_ranges, binary=binary,
                                     style=style)
      yield '{0!s} {1!s}\t{2!s}'.format(mode, blob_id, filename)
  return create_tree(index_info_generator(), '--index-info')


def create_tree(input_lines, mode):
  """Create a tree object from the given input.

  If mode is '--stdin', it must be a list of filenames.  If mode is
  '--index-info' is must be a list of values suitable for "git update-index
  --index-info", such as "<mode> <SP> <sha1> <TAB> <filename>".  Any other mode
  is invalid."""
  assert mode in ('--stdin', '--index-info')
  cmd = ['git', 'update-index', '--add', '-z', mode]
  with temporary_index_file():
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for line in input_lines:
      p.stdin.write('{0!s}\0'.format(line))
    p.stdin.close()
    if p.wait() != 0:
      die('`{0!s}` failed'.format(' '.join(cmd)))
    tree_id = run('git', 'write-tree')
    return tree_id


def clang_format_to_blob(filename, line_ranges, binary='clang-format',
                         style=None):
  """Run clang-format on the given file and save the result to a git blob.

  Returns the object ID (SHA-1) of the created blob."""
  clang_format_cmd = [binary, filename]
  if style:
    clang_format_cmd.extend(['-style='+style])
  clang_format_cmd.extend([
      '-lines={0!s}:{1!s}'.format(start_line, start_line+line_count-1)
      for start_line, line_count in line_ranges])
  try:
    clang_format = subprocess.Popen(clang_format_cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE)
  except OSError as e:
    if e.errno == errno.ENOENT:
      die('cannot find executable "{0!s}"'.format(binary))
    else:
      raise
  clang_format.stdin.close()
  hash_object_cmd = ['git', 'hash-object', '-w', '--path='+filename, '--stdin']
  hash_object = subprocess.Popen(hash_object_cmd, stdin=clang_format.stdout,
                                 stdout=subprocess.PIPE)
  clang_format.stdout.close()
  stdout = hash_object.communicate()[0]
  if hash_object.returncode != 0:
    die('`{0!s}` failed'.format(' '.join(hash_object_cmd)))
  if clang_format.wait() != 0:
    die('`{0!s}` failed'.format(' '.join(clang_format_cmd)))
  return stdout.rstrip('\r\n')


@contextlib.contextmanager
def temporary_index_file(tree=None):
  """Context manager for setting GIT_INDEX_FILE to a temporary file and deleting
  the file afterward."""
  index_path = create_temporary_index(tree)
  old_index_path = os.environ.get('GIT_INDEX_FILE')
  os.environ['GIT_INDEX_FILE'] = index_path
  try:
    yield
  finally:
    if old_index_path is None:
      del os.environ['GIT_INDEX_FILE']
    else:
      os.environ['GIT_INDEX_FILE'] = old_index_path
    os.remove(index_path)


def create_temporary_index(tree=None):
  """Create a temporary index file and return the created file's path.

  If `tree` is not None, use that as the tree to read in.  Otherwise, an
  empty index is created."""
  gitdir = run('git', 'rev-parse', '--git-dir')
  path = os.path.join(gitdir, temp_index_basename)
  if tree is None:
    tree = '--empty'
  run('git', 'read-tree', '--index-output='+path, tree)
  return path


def print_diff(old_tree, new_tree):
  """Print the diff between the two trees to stdout."""
  # We use the porcelain 'diff' and not plumbing 'diff-tree' because the output
  # is expected to be viewed by the user, and only the former does nice things
  # like color and pagination.
  subprocess.check_call(['git', 'diff', old_tree, new_tree, '--'])


def apply_changes(old_tree, new_tree, force=False, patch_mode=False):
  """Apply the changes in `new_tree` to the working directory.

  Bails if there are local changes in those files and not `force`.  If
  `patch_mode`, runs `git checkout --patch` to select hunks interactively."""
  changed_files = run('git', 'diff-tree', '-r', '-z', '--name-only', old_tree,
                      new_tree).rstrip('\0').split('\0')
  if not force:
    unstaged_files = run('git', 'diff-files', '--name-status', *changed_files)
    if unstaged_files:
      print >>sys.stderr, ('The following files would be modified but '
                           'have unstaged changes:')
      print >>sys.stderr, unstaged_files
      print >>sys.stderr, 'Please commit, stage, or stash them first.'
      sys.exit(2)
  if patch_mode:
    # In patch mode, we could just as well create an index from the new tree
    # and checkout from that, but then the user will be presented with a
    # message saying "Discard ... from worktree".  Instead, we use the old
    # tree as the index and checkout from new_tree, which gives the slightly
    # better message, "Apply ... to index and worktree".  This is not quite
    # right, since it won't be applied to the user's index, but oh well.
    with temporary_index_file(old_tree):
      subprocess.check_call(['git', 'checkout', '--patch', new_tree])
    index_tree = old_tree
  else:
    with temporary_index_file(new_tree):
      run('git', 'checkout-index', '-a', '-f')
  return changed_files


def run(*args, **kwargs):
  stdin = kwargs.pop('stdin', '')
  verbose = kwargs.pop('verbose', True)
  strip = kwargs.pop('strip', True)
  for name in kwargs:
    raise TypeError("run() got an unexpected keyword argument '{0!s}'".format(name))
  p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       stdin=subprocess.PIPE)
  stdout, stderr = p.communicate(input=stdin)
  if p.returncode == 0:
    if stderr:
      if verbose:
        print >>sys.stderr, '`{0!s}` printed to stderr:'.format(' '.join(args))
      print >>sys.stderr, stderr.rstrip()
    if strip:
      stdout = stdout.rstrip('\r\n')
    return stdout
  if verbose:
    print >>sys.stderr, '`{0!s}` returned {1!s}'.format(' '.join(args), p.returncode)
  if stderr:
    print >>sys.stderr, stderr.rstrip()
  sys.exit(2)


def die(message):
  print >>sys.stderr, 'error:', message
  sys.exit(2)


if __name__ == '__main__':
  main()

