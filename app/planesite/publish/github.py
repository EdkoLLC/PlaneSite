"""GitHub as a publish destination: one public repository per map, served by
raw.githubusercontent.com/<owner>/<repo>/main/{z}/{x}/{y}.png for the XAG One app.

Sign-in is a GitHub App ("PlaneSite") with the device flow: PlaneSite shows a short code, the user
approves it on github.com, and PlaneSite gets a user token. No client secret is built in (device
flow tokens can be refreshed without one). The token (8 hours) and its refresh token (6 months) are
kept in Windows Credential Manager and refreshed when needed. The app needs, per repository:
Administration (write) to create repositories and Contents (write) to push. It works in every
account or organisation it's installed on that the signed-in user can reach.

Pushing uses dulwich (pure-Python git, bundled in app/vendor) over HTTPS. The commit objects are
built here directly from the tile files, without a local .git folder: each upload part is one
commit on top of the previous one, pushed before the next is built, and a record of what was
pushed lets an interrupted publish resume where it stopped.
"""
import hashlib
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import Destination, PublishError, describe_batch, load_state, plan_batches, save_state, scan_tiles, use_vendor
from .. import credstore

API = os.environ.get("PLANESITE_GITHUB_API", "https://api.github.com")
WEB = os.environ.get("PLANESITE_GITHUB_WEB", "https://github.com")
RAW = "https://raw.githubusercontent.com"
CRED = "github-user-token"
# The registered GitHub App (not secret). Settings can override them ("github_app" in config).
DEFAULT_APP = {"client_id": "Iv23li7SRxKFAMsZaB0H", "slug": "planesite-edko"}   # EdkoLLC's "PlaneSite Edko" app (not secret)
_UA = "PlaneSite/2.0"


def _ctx():
    return ssl.create_default_context()


class GitHub(Destination):
    id = "github"
    label = "GitHub"

    def __init__(self, cfg):
        self.cfg = cfg
        self.flow = None
        self._targets = {}

    # ------------------------------------------------------------ settings / http
    def app(self):
        a = dict(DEFAULT_APP)
        a.update({k: v for k, v in (self.cfg.get("github_app") or {}).items() if v})
        return a

    def install_url(self):
        slug = self.app().get("slug")
        return "%s/apps/%s/installations/new" % (WEB, slug) if slug else None

    def _req(self, method, url, body=None, token=None, form=False):
        """(status, parsed json or None)."""
        headers = {"User-Agent": _UA, "Accept": "application/vnd.github+json" if not form else "application/json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        data = None
        if body is not None:
            if form:
                data = urllib.parse.urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30, context=_ctx()) as r:
                raw = r.read()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                return e.code, json.loads(raw) if raw else None
            except ValueError:
                return e.code, None
        except (urllib.error.URLError, OSError) as e:
            raise PublishError("Couldn't reach GitHub (%s). Check the internet connection." % getattr(e, "reason", e))

    def _api(self, method, path, body=None):
        return self._req(method, API + path, body, token=self.token())

    # ------------------------------------------------------------ sign-in
    def _load(self):
        raw = credstore.load(CRED)
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _save(self, t):
        credstore.save(CRED, json.dumps(t) if t else "", "PlaneSite: GitHub sign-in")

    @staticmethod
    def _grant(r, login=None):
        now = time.time()
        return {"access_token": r["access_token"],
                "expires_at": now + int(r["expires_in"]) if r.get("expires_in") else None,
                "refresh_token": r.get("refresh_token"),
                "refresh_expires_at": now + int(r["refresh_token_expires_in"]) if r.get("refresh_token_expires_in") else None,
                "login": login}

    def token(self):
        """A valid user token, refreshed when it's about to expire."""
        t = self._load()
        if not t:
            raise PublishError("Sign in with GitHub first.")
        if t.get("expires_at") and time.time() > t["expires_at"] - 300:
            if t.get("refresh_token") and time.time() < (t.get("refresh_expires_at") or 0):
                st, r = self._req("POST", WEB + "/login/oauth/access_token",
                                  {"client_id": self.app()["client_id"], "grant_type": "refresh_token",
                                   "refresh_token": t["refresh_token"]}, form=True)
                if r and r.get("access_token"):
                    t = self._grant(r, t.get("login"))
                    self._save(t)
                    return t["access_token"]
            self._save(None)
            raise PublishError("Your GitHub sign-in has expired. Sign in again.")
        return t["access_token"]

    def start_signin(self):
        cid = self.app().get("client_id")
        if not cid:
            raise PublishError("PlaneSite's GitHub App isn't set up yet: enter its Client ID under GitHub App settings.")
        st, r = self._req("POST", WEB + "/login/device/code", {"client_id": cid}, form=True)
        if not r or "device_code" not in r:
            msg = (r or {}).get("error_description") or (r or {}).get("error") or "HTTP %s" % st
            if (r or {}).get("error") == "device_flow_disabled":
                msg = "Device sign-in is turned off for the GitHub App. Turn on 'Enable Device Flow' in its settings."
            raise PublishError("GitHub didn't start the sign-in: %s" % msg)
        flow = {"state": "waiting", "user_code": r["user_code"], "verification_uri": r.get("verification_uri") or WEB + "/login/device",
                "expires_at": time.time() + int(r.get("expires_in") or 900), "interval": int(r.get("interval") or 5),
                "device_code": r["device_code"]}
        self.flow = flow
        threading.Thread(target=self._poll, args=(flow, cid), daemon=True).start()
        return self.public_flow()

    def _poll(self, flow, cid):
        interval = flow["interval"]
        while self.flow is flow and time.time() < flow["expires_at"]:
            time.sleep(interval)
            try:
                st, r = self._req("POST", WEB + "/login/oauth/access_token",
                                  {"client_id": cid, "device_code": flow["device_code"],
                                   "grant_type": "urn:ietf:params:oauth:grant-type:device_code"}, form=True)
            except PublishError:
                continue                                   # a dropped connection: keep waiting
            r = r or {}
            err = r.get("error")
            if err == "authorization_pending":
                continue
            if err == "slow_down":
                interval = int(r.get("interval") or interval + 5)
                continue
            if err:
                flow["state"] = "error"
                flow["message"] = {"expired_token": "The code expired. Start the sign-in again.",
                                   "access_denied": "Sign-in was cancelled on GitHub."}.get(err, r.get("error_description") or err)
                return
            t = self._grant(r)
            st, me = self._req("GET", API + "/user", token=t["access_token"])
            t["login"] = (me or {}).get("login")
            self._save(t)
            flow["state"] = "done"
            flow["user"] = t["login"]
            return
        if self.flow is flow and flow["state"] == "waiting":
            flow["state"] = "error"
            flow["message"] = "The code expired. Start the sign-in again."

    def public_flow(self):
        f = self.flow
        if not f:
            return None
        return {k: f.get(k) for k in ("state", "user_code", "verification_uri", "message", "user")} | \
            {"seconds_left": max(0, int(f["expires_at"] - time.time()))}

    def signout(self):
        self.flow = None
        self._targets = {}
        self._save(None)

    def status(self):
        t = self._load()
        return {"id": self.id, "label": self.label, "configured": bool(self.app().get("client_id")),
                "ready": bool(t), "user": (t or {}).get("login"), "flow": self.public_flow(),
                "install_url": self.install_url(), "app": self.app()}

    # ------------------------------------------------------------ where to publish
    def targets(self):
        out, page = [], 1
        while True:
            st, r = self._api("GET", "/user/installations?per_page=100&page=%d" % page)
            if st == 401:
                self._save(None)
                raise PublishError("GitHub no longer accepts this sign-in. Sign in again.")
            if st != 200:
                raise PublishError("GitHub didn't list the accounts (HTTP %s)." % st)
            inst = (r or {}).get("installations") or []
            for i in inst:
                acc = i.get("account") or {}
                perms = i.get("permissions") or {}
                ok = perms.get("administration") == "write" and perms.get("contents") == "write"
                note = ""
                if not ok:
                    note = "The app there lacks Administration and Contents write access: an owner must accept the app's permissions."
                elif i.get("repository_selection") == "selected":
                    note = "Installed for selected repositories only: new maps may be refused. An owner can switch it to All repositories."
                out.append({"id": acc.get("login"), "label": acc.get("login"), "type": acc.get("type"), "ok": ok,
                            "note": note, "selection": i.get("repository_selection")})
            if len(inst) < 100:
                break
            page += 1
        self._targets = {t["id"].lower(): t for t in out if t["id"]}
        me = (self._load() or {}).get("login")
        return {"targets": out, "add_url": self.install_url(), "user": me}

    def tile_url(self, target, name):
        return "%s/%s/%s/main/{z}/{x}/{y}.png" % (RAW, target, name)

    def _head(self, target, name):
        st, r = self._api("GET", "/repos/%s/%s/git/ref/heads/main" % (target, name))
        if st == 200 and r and (r.get("object") or {}).get("sha"):
            return r["object"]["sha"]
        return None

    def check(self, target, name, folder):
        st, r = self._api("GET", "/repos/%s/%s" % (target, name))
        if st == 404:
            return {"exists": False, "resumable": False}
        if st != 200:
            raise PublishError("GitHub didn't answer about %s/%s (HTTP %s)." % (target, name, st))
        head = self._head(target, name)
        s = load_state(self.id, target, name)
        resumable = bool(s and head and s.get("head") == head and s.get("folder") == folder
                         and s.get("done", 0) < s.get("total", 0))
        return {"exists": True, "empty": head is None, "resumable": resumable,
                "done": (s or {}).get("done", 0), "total": (s or {}).get("total", 0),
                "html_url": (r or {}).get("html_url")}

    # ------------------------------------------------------------ publish
    def _create(self, target, name, login):
        t = self._targets.get(target.lower()) or {}
        body = {"name": name, "private": False, "auto_init": False, "has_issues": False, "has_wiki": False,
                "has_projects": False, "description": "XAG One base map tiles, made with PlaneSite"}
        if t.get("type") == "User" or (not t and login and target.lower() == login.lower()):
            if not login or target.lower() != login.lower():
                raise PublishError("PlaneSite can only create repositories in your own account (%s) or in an "
                                   "organisation the app is installed on." % login)
            st, r = self._api("POST", "/user/repos", body)
        else:
            st, r = self._api("POST", "/orgs/%s/repos" % target, body)
        if st == 201:
            for _ in range(20):                    # a new repository can take a moment to appear
                if self._api("GET", "/repos/%s/%s" % (target, name))[0] == 200:
                    return
                time.sleep(0.5)
            return
        msg = " ".join(e.get("message", "") for e in (r or {}).get("errors") or []) or (r or {}).get("message") or ""
        if st == 422 and "already exists" in msg:
            raise PublishError("A repository named %s already exists in %s." % (name, target))
        if st in (403, 404):
            raise PublishError("GitHub refused to create %s/%s (%s). The PlaneSite app may not be installed there "
                               "with Administration write access, or you may not be allowed to create repositories "
                               "there." % (target, name, msg or "HTTP %s" % st))
        raise PublishError("GitHub refused to create %s/%s: %s" % (target, name, msg or "HTTP %s" % st))

    def _keep_entries(self, target, name, head):
        """Top-level files of the existing repository other than zoom folders (README ...)."""
        if not head:
            return []
        st, c = self._api("GET", "/repos/%s/%s/git/commits/%s" % (target, name, head))
        if st != 200:
            raise PublishError("Couldn't read the repository's latest commit (HTTP %s)." % st)
        st, t = self._api("GET", "/repos/%s/%s/git/trees/%s" % (target, name, c["tree"]["sha"]))
        if st != 200:
            raise PublishError("Couldn't read the repository's files (HTTP %s)." % st)
        return [(e["path"], int(e["mode"], 8), e["sha"]) for e in t.get("tree") or [] if not e["path"].isdigit()]

    def _tree(self, target, name, sha, recursive=False):
        st, t = self._api("GET", "/repos/%s/%s/git/trees/%s%s" % (target, name, sha, "?recursive=1" if recursive else ""))
        if st != 200:
            raise PublishError("Couldn't read the repository's files (HTTP %s)." % st)
        return t

    def _remote_tiles(self, target, name, head):
        """{(z, x, y): (blob sha, size)} of the tiles already in the repository's main branch.
        One request per zoom folder; a folder too big for one listing is read column by column."""
        if not head:
            return {}
        st, c = self._api("GET", "/repos/%s/%s/git/commits/%s" % (target, name, head))
        if st != 200:
            raise PublishError("Couldn't read the repository's latest commit (HTTP %s)." % st)
        out = {}

        def add(z, x, entries):
            for e in entries:
                n = e["path"]
                if e["type"] == "blob" and n.endswith(".png") and n[:-4].isdigit():
                    out[(z, x, int(n[:-4]))] = (e["sha"], int(e.get("size") or 0))

        for ze in self._tree(target, name, c["tree"]["sha"]).get("tree") or []:
            if not (ze["type"] == "tree" and ze["path"].isdigit()):
                continue
            z = int(ze["path"])
            t = self._tree(target, name, ze["sha"], recursive=True)
            if not t.get("truncated"):
                cols = {}
                for e in t.get("tree") or []:
                    parts = e["path"].split("/")
                    if len(parts) == 2 and parts[0].isdigit():
                        cols.setdefault(int(parts[0]), []).append(dict(e, path=parts[1]))
                for x, ents in cols.items():
                    add(z, x, ents)
                continue
            for xe in self._tree(target, name, ze["sha"]).get("tree") or []:
                if xe["type"] == "tree" and xe["path"].isdigit():
                    add(z, int(xe["path"]), self._tree(target, name, xe["sha"]).get("tree") or [])
        return out

    def _delete(self, target, name):
        """Delete the repository (to publish a map again without keeping the old tiles in its history)."""
        st, r = self._api("DELETE", "/repos/%s/%s" % (target, name))
        if st not in (204, 404):
            msg = (r or {}).get("message") or "HTTP %s" % st
            raise PublishError("GitHub refused to delete %s/%s to start it over (%s). An owner of %s may need "
                               "to allow members to delete repositories, or delete it on github.com."
                               % (target, name, msg, target))
        for _ in range(40):
            if self._api("GET", "/repos/%s/%s" % (target, name))[0] == 404:
                return
            time.sleep(0.5)

    # How much changed content "Update" may add to the history before PlaneSite asks first: GitHub keeps
    # every earlier version of a tile, so re-publishing a re-made map would double the repository.
    HISTORY_ASK_BYTES = 200_000_000
    HISTORY_ASK_SHARE = 0.25

    def publish(self, folder, target, name, mode, progress, cancel):
        """mode: "new" (create), "into" (update an existing repository: only new and changed tiles go up;
        asks first when that would leave a lot of replaced tiles in the history), "into_force" (update
        without asking), "resume" (same as into_force), "replace" (delete the repository and publish
        into a fresh one at the same address)."""
        use_vendor()
        from dulwich.client import Urllib3HttpGitClient
        from dulwich.objects import Blob, Commit, Tree
        from dulwich.pack import full_unpacked_object

        tok = self.token()
        login = (self._load() or {}).get("login") or "PlaneSite"
        tiles = scan_tiles(folder)
        if not tiles:
            raise PublishError("No tiles in %s. Make the XAG tiles first." % folder)
        total_all = sum(t[4] for t in tiles)
        if not self._targets:
            try:
                self.targets()
            except PublishError:
                pass
        progress({"phase": "preparing", "part": 0, "parts": 0, "sent": 0, "total": total_all})
        if mode == "replace":
            self._delete(target, name)
            save_state(self.id, target, name, None)
            mode = "new"
        if mode == "new":
            self._create(target, name, login)
        head = self._head(target, name)

        # what's on GitHub already, and which local tiles differ from it
        remote = self._remote_tiles(target, name, head) if head else {}

        def blob_id(p):
            with open(p, "rb") as f:
                data = f.read()
            return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()

        ids, done_b, last = {}, [0], [0.0]
        for z, x, y, p, sz in tiles:
            if cancel():
                return {"stopped": True, "done": 0, "parts": 0}
            ids[(z, x, y)] = blob_id(p)
            done_b[0] += sz
            if time.time() - last[0] > 0.3:
                last[0] = time.time()
                progress({"phase": "comparing", "part": 0, "parts": 0, "sent": done_b[0], "total": total_all})
        todo = [t for t in tiles if (remote.get(t[:3]) or (None,))[0] != ids[t[:3]]]
        removed = [k for k in remote if k not in ids]
        changed = [t for t in todo if t[:3] in remote]
        changed_bytes = sum(remote[t[:3]][1] for t in changed)
        remote_bytes = sum(v[1] for v in remote.values())
        stats = {"same": len(tiles) - len(todo), "new": len(todo) - len(changed), "changed": len(changed),
                 "removed": len(removed), "changed_bytes": changed_bytes, "remote_bytes": remote_bytes,
                 "upload_bytes": sum(t[4] for t in todo)}
        base = {"url": self.tile_url(target, name), "repo_url": "%s/%s/%s" % (WEB, target, name),
                "tiles": len(tiles), "bytes": total_all}
        if head and not todo and not removed:
            save_state(self.id, target, name, None)
            progress({"phase": "done", "part": 0, "parts": 0, "sent": 0, "total": 0})
            return dict(base, unchanged=True, parts=0, **stats)
        if mode == "into" and changed_bytes > self.HISTORY_ASK_BYTES and \
                changed_bytes > self.HISTORY_ASK_SHARE * max(remote_bytes, 1):
            return dict(base, confirm=True, **stats)

        keep = self._keep_entries(target, name, head)
        readme = None
        if not any(k[0].lower() == "readme.md" for k in keep):
            readme = Blob.from_string(("# %s\n\nXAG One base map tiles, made with PlaneSite.\n\nTile address:\n\n    %s\n"
                                       % (name, self.tile_url(target, name))).encode())
        batches = plan_batches(todo) or [[]]           # nothing to upload but removals: one commit
        total = stats["upload_bytes"]
        client = Urllib3HttpGitClient(WEB + "/", username="x-access-token", password=tok)
        path = "/%s/%s.git" % (target, name)
        # the tree starts as what's on GitHub (so the map stays whole between parts); each part swaps
        # in its new and changed tiles, and the last part drops tiles the map no longer has
        zdir = {}
        for (z, x, y), (sha, _) in remote.items():
            zdir.setdefault(z, {}).setdefault(x, {})[y] = sha.encode()
        sent_bytes = [0]
        sent_trees = set()
        for i in range(len(batches)):
            if cancel():
                progress({"phase": "stopped", "part": i, "parts": len(batches), "sent": sent_bytes[0], "total": total})
                return {"stopped": True, "done": i, "parts": len(batches)}
            b = batches[i]
            progress({"phase": "preparing", "part": i + 1, "parts": len(batches), "sent": sent_bytes[0], "total": total})
            for z, x, y, p, sz in b:
                zdir.setdefault(z, {}).setdefault(x, {})[y] = ids[(z, x, y)].encode()
            if i == len(batches) - 1:
                for z, x, y in removed:
                    col = zdir.get(z, {}).get(x)
                    if col is not None:
                        col.pop(y, None)
                        if not col:
                            zdir[z].pop(x)
                            if not zdir[z]:
                                zdir.pop(z)
            root = Tree()
            trees = []
            for nm, md, sha in keep:
                root.add(nm.encode(), md, sha.encode())
            if readme is not None:
                root.add(b"README.md", 0o100644, readme.id)
            for z in sorted(zdir):
                zt = Tree()
                for x in sorted(zdir[z]):
                    xt = Tree()
                    for y, bid in zdir[z][x].items():
                        xt.add(b"%d.png" % y, 0o100644, bid)
                    trees.append(xt)
                    zt.add(str(x).encode(), 0o040000, xt.id)
                trees.append(zt)
                root.add(str(z).encode(), 0o040000, zt.id)
            trees.append(root)
            new_trees = [t for t in trees if t.id not in sent_trees]
            c = Commit()
            c.tree = root.id
            c.parents = [head.encode()] if head else []
            who = ("%s <%s@users.noreply.github.com>" % (login, login)).encode()
            c.author = c.committer = who
            c.author_time = c.commit_time = int(time.time())
            c.author_timezone = c.commit_timezone = 0
            what = describe_batch(b) if b else "removed tiles"
            c.message = ("Map tiles: %s (part %d of %d)\n\nMade with PlaneSite.\n"
                         % (what, i + 1, len(batches))).encode()
            extra = [readme] if (readme is not None and i == 0) else []
            expected = head.encode() if head else None
            part = i + 1

            def update_refs(refs, expected=expected, cid=c.id):
                cur = refs.get(b"refs/heads/main")
                if (cur or None) != expected:
                    raise PublishError("The repository changed on GitHub during the publish. Publish it again.")
                return {b"refs/heads/main": cid}

            def generate_pack_data(have, want, ofs_delta=False, progress=None, b=b, new_trees=new_trees, c=c,
                                   extra=extra, part=part, **kw):
                def objs():
                    seen = set()
                    for z, x, y, p, sz in b:
                        if cancel():
                            raise PublishError("Stopped.")
                        bid = ids[(z, x, y)]
                        if bid not in seen:              # identical tiles (e.g. plain field) go up once
                            seen.add(bid)
                            with open(p, "rb") as f:
                                yield full_unpacked_object(Blob.from_string(f.read()))
                        sent_bytes[0] += sz
                        tick(part)
                    for o in extra + new_trees + [c]:
                        yield full_unpacked_object(o)
                n_blobs = len({ids[t[:3]] for t in b})
                return n_blobs + len(extra) + len(new_trees) + 1, objs()

            lastp = [0.0]

            def tick(part):
                if time.time() - lastp[0] > 0.3:
                    lastp[0] = time.time()
                    progress({"phase": "uploading", "part": part, "parts": len(batches), "sent": sent_bytes[0],
                              "total": total})

            progress({"phase": "uploading", "part": part, "parts": len(batches), "sent": sent_bytes[0], "total": total})
            try:
                res = client.send_pack(path, update_refs, generate_pack_data)
            except PublishError:
                raise
            except Exception as e:
                msg = str(e)
                if "401" in msg or "403" in msg or "Unauthorized" in type(e).__name__:
                    msg = "GitHub refused the upload (no write access to %s/%s)." % (target, name)
                raise PublishError("Upload part %d of %d failed: %s. Publish again to continue: tiles already "
                                   "on GitHub aren't uploaded again." % (part, len(batches), msg))
            errs = {k: v for k, v in (getattr(res, "ref_status", None) or {}).items() if v}
            if errs:
                raise PublishError("GitHub rejected part %d of %d: %s" % (part, len(batches), "; ".join(
                    "%s %s" % (k.decode(errors="replace"), v) for k, v in errs.items())))
            sent_trees.update(t.id for t in new_trees)
            head = c.id.decode()
            save_state(self.id, target, name, {"head": head, "done": part, "total": len(batches), "folder": folder,
                                               "at": time.strftime("%Y-%m-%d %H:%M")})
        save_state(self.id, target, name, None)
        progress({"phase": "done", "part": len(batches), "parts": len(batches), "sent": total, "total": total})
        return dict(base, parts=len(batches), **stats)
