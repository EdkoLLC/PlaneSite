"""Secrets kept for this user in Windows Credential Manager or the macOS Keychain (a private file
elsewhere, for development on other systems). Never in the project or the settings file."""
import os
import sys


def _file(name):
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "PlaneSite", "secret_" + name.replace("/", "_"))


def _mac_save(service, secret):
    import subprocess
    if not secret:
        subprocess.run(["security", "delete-generic-password", "-a", "PlaneSite", "-s", service],
                       capture_output=True)
        return
    # through security's own prompt on stdin, so the secret isn't on a command line
    q = lambda v: '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'   # noqa: E731
    cmd = "add-generic-password -U -a PlaneSite -s %s -l %s -w %s\n" % (q(service), q(service), q(secret))
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise OSError("Couldn't save to the Keychain: %s" % (r.stderr or r.stdout).strip())


def _mac_load(service):
    import subprocess
    r = subprocess.run(["security", "find-generic-password", "-a", "PlaneSite", "-s", service, "-w"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.rstrip("\n") or None


def save(name, secret, comment="PlaneSite"):
    """Store (or with an empty secret, delete) the secret under PlaneSite/<name>."""
    if sys.platform == "win32":
        from .pilot2 import _win_cred_write
        _win_cred_write("PlaneSite/" + name, secret or "", comment)
        return
    if sys.platform == "darwin":
        _mac_save("PlaneSite/" + name, secret)
        return
    p = _file(name)
    if not secret:
        if os.path.exists(p):
            os.remove(p)
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)


def load(name):
    if sys.platform == "win32":
        from .pilot2 import _win_cred_read
        return _win_cred_read("PlaneSite/" + name)
    if sys.platform == "darwin":
        return _mac_load("PlaneSite/" + name)
    try:
        with open(_file(name)) as f:
            return f.read() or None
    except OSError:
        return None
