"""Thin wrapper around the COPR HTTP API.

All functions accept explicit (login, token) so each job uses the
credentials of the user who submitted it.
"""
import os
import time

import requests
import urllib3

COPR_API_URL = os.environ.get("COPR_API_URL", "http://copr-frontend:5000")
VERIFY_SSL   = os.environ.get("COPR_API_VERIFY_SSL", "true").lower() not in ("false", "0", "no")

if not VERIFY_SSL:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _auth(login, token):
    return (login, token)


def submit_srpm_upload(owner, coprname, srpm_path, login, token, chroots=None):
    """Upload an SRPM file and submit a build. Returns (build_id, error_str)."""
    data = {"ownername": owner, "projectname": coprname}
    if chroots:
        # multipart: repeat field for each chroot
        for ch in chroots:
            data.setdefault("chroots", [])
            if isinstance(data["chroots"], list):
                data["chroots"].append(ch)

    with open(srpm_path, "rb") as f:
        # requests handles list values in data as repeated fields
        form_data = []
        for k, v in data.items():
            if isinstance(v, list):
                for item in v:
                    form_data.append((k, item))
            else:
                form_data.append((k, v))

        resp = requests.post(
            "{}/api/v3/build/create/upload".format(COPR_API_URL),
            auth=_auth(login, token),
            verify=VERIFY_SSL,
            data=form_data,
            files={"pkgs": (srpm_path.name, f, "application/x-rpm")},
            timeout=120,
        )
    if resp.ok:
        return resp.json().get("id"), None
    return None, "HTTP {}: {}".format(resp.status_code, resp.text[:300])


def submit_scm_build(owner, coprname, clone_url, login, token, committish="", spec=""):
    """Submit a build from an SCM source. Returns (build_id, error_str)."""
    resp = requests.post(
        "{}/api/v3/build/create/scm".format(COPR_API_URL),
        auth=_auth(login, token),
        verify=VERIFY_SSL,
        json={
            "ownername":         owner,
            "projectname":       coprname,
            "scmtype":           "git",
            "clone_url":         clone_url,
            "committish":        committish,
            "spec":              spec,
            "srpm_build_method": "rpkg",
        },
        timeout=30,
    )
    if resp.ok:
        return resp.json().get("id"), None
    return None, "HTTP {}: {}".format(resp.status_code, resp.text[:300])


def get_build(build_id, login, token):
    """Return build dict or raise."""
    resp = requests.get(
        "{}/api_3/build/{}".format(COPR_API_URL, build_id),
        auth=_auth(login, token),
        verify=VERIFY_SSL,
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def poll_build_until_done(build_id, login, token, log_fn, max_wait=3600, interval=10):
    """Poll build status every `interval` seconds. Returns final state string."""
    terminal   = {"succeeded", "failed", "canceled", "skipped"}
    deadline   = time.time() + max_wait
    last_state = "unknown"

    while time.time() < deadline:
        try:
            data  = get_build(build_id, login, token)
            state = data.get("state", "unknown")
            if state != last_state:
                log_fn("  构建状态: {}".format(state))
                last_state = state
            if state in terminal:
                return state
        except Exception as exc:
            log_fn("  轮询出错: {}".format(exc))
        time.sleep(interval)

    log_fn("  构建超时（超过 1 小时）")
    return "failed"
