"""Seed ComfyUI's per-user frontend settings (user/default/comfy.settings.json) without touching anything else in it.

    python3 comfy_settings.py <comfy.settings.json> [--dry]

3.17.1 (error log row 81): frontend 1.55.x opens a full-screen "Welcome to Apps" tour over the App Mode form on a user's
first visit; its scrim takes every click until it is dismissed, and its text is for app builders, not customers.
First-party sources, ComfyUI_frontend v1.55.17:
  - src/platform/onboarding/onboardingTours.ts: TOUR_SEEN_SETTING = 'Comfy.OnboardingCoachmarks.Seen',
    ENTRY_PATHS = ['appMode', 'firstRun']; coreSettings.ts declares it a hidden Array<string>, default [].
  - onboardingTourStore.ts: startTour(entryPath) returns early when the list includes the entry path; markTourSeen
    appends the path to the list it read (`[...seen, entryPath]`), which is exactly the merge below.
  - useTourTriggers.ts: only 'appMode' opens on its own; 'firstRun' waits on a feature flag ComfyUI's server does not
    serve. So ["appMode"] alone.
ComfyUI v0.41.2 app/app_settings.py reads this file on every request and writes the whole file back on each
POST /settings/{id}; a file it cannot parse is served as {} and then overwritten by the next save. So the write here is
atomic (a temp file in the same folder, then os.replace), and a file that is not a JSON object is left untouched.

Prints one line: "seeded: <keys>", "already: <keys>", "would seed: <keys>" or "left untouched: <why>".
Exit 0, or 3 when the file exists but is not a JSON object."""
import json, os, sys, tempfile

SEED = {"Comfy.OnboardingCoachmarks.Seen": ["appMode"]}


def merge(settings, seed=SEED):
    """(merged, changed keys). A list in the seed: a missing key gets the seed's list; an existing list keeps its items and
    order and gains the missing ones at the end (as the frontend's markTourSeen does); a value that is not a list is
    replaced by the seed's list (the frontend calls .includes on it, and a non-list breaks its store). Every other key is
    left exactly as it is."""
    out = dict(settings); changed = []
    for key, want in seed.items():
        have = out.get(key)
        if isinstance(want, list):
            if not isinstance(have, list):
                out[key] = list(want); changed.append(key); continue
            add = [v for v in want if v not in have]
            if add:
                out[key] = have + add; changed.append(key)
        elif have != want:
            out[key] = want; changed.append(key)
    return out, changed


def seed_file(path, dry=False, seed=SEED):
    """(line, exit code) for one settings file."""
    settings = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                settings = json.load(f)
        except (OSError, ValueError) as e:
            return "left untouched: %s is not readable JSON (%s)" % (path, e.__class__.__name__), 3
        if not isinstance(settings, dict):
            return "left untouched: %s holds a JSON %s, not an object" % (path, type(settings).__name__), 3
    merged, changed = merge(settings, seed)
    if not changed:
        return "already: %s" % ", ".join(seed), 0
    if dry:
        return "would seed: %s" % ", ".join(changed), 0
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".comfy.settings.", suffix=".json", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(merged, indent=4))                     # ComfyUI's own save writes indent=4
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return "seeded: %s" % ", ".join(changed), 0


def main(argv):
    args = [a for a in argv if a != "--dry"]
    if len(args) != 1:
        print("usage: comfy_settings.py <comfy.settings.json> [--dry]", file=sys.stderr); return 2
    line, rc = seed_file(args[0], dry="--dry" in argv)
    print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
