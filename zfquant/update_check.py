"""Check GitHub for a newer version of this plugin than what's installed.

Jython-only (java.net.* / subprocess). Mirrors check_updates.command's
logic exactly -- a git checkout compares HEAD against origin/main; a zip
download compares a baked-in VERSION file against GitHub's API -- so
whichever way an operator checks (Finder or from inside Fiji), the answer
agrees. Never raises: a network hiccup or an unusual install layout should
never be able to stop the plugin from launching, so every failure mode
here just means "nothing to report."
"""

import json
import os
import subprocess

REPO = "marjvilla/zFishEpiFloQuant"
REPO_URL = "https://github.com/" + REPO
_TIMEOUT_MS = 2500


def _repo_root():
    """This file lives at <repo>/zfquant/update_check.py -- but install.sh
    symlinks the whole zfquant/ folder into <Fiji>/jars/Lib/, so when this
    runs for real __file__ is reached through that symlink. realpath (not
    abspath, which leaves symlinks alone) is what actually lands back on
    the real repo checkout, where .git/VERSION live."""
    return os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def _local_sha(root):
    git_dir = os.path.join(root, ".git")
    if os.path.isdir(git_dir):
        try:
            process = subprocess.Popen(
                ["git", "rev-parse", "HEAD"], cwd=root,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            out, _ = process.communicate()
            if process.returncode == 0:
                text = out.decode("utf-8") if isinstance(out, bytes) else out
                return text.strip()
        except Exception:
            pass
        return None
    version_file = os.path.join(root, "VERSION")
    if os.path.isfile(version_file):
        handle = open(version_file, "r")
        try:
            return handle.read().strip()
        finally:
            handle.close()
    return None


def _remote_sha():
    from java.net import URL
    connection = URL(
        "https://api.github.com/repos/%s/commits/main" % REPO).openConnection()
    connection.setConnectTimeout(_TIMEOUT_MS)
    connection.setReadTimeout(_TIMEOUT_MS)
    connection.setRequestProperty("Accept", "application/vnd.github+json")
    stream = connection.getInputStream()
    try:
        from java.io import BufferedReader, InputStreamReader
        reader = BufferedReader(InputStreamReader(stream, "UTF-8"))
        lines = []
        line = reader.readLine()
        while line is not None:
            lines.append(line)
            line = reader.readLine()
    finally:
        stream.close()
    data = json.loads("\n".join(lines))
    return data.get("sha")


def check_for_update():
    """A short status string worth showing the operator, or None if
    there's nothing worth saying (up to date, or the check couldn't
    complete at all)."""
    try:
        local = _local_sha(_repo_root())
        remote = _remote_sha()
    except Exception:
        return None
    if not remote or not local or local == remote:
        return None
    return "A newer version of Zebrafish Quant is available: " + REPO_URL
