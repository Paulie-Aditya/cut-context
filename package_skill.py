"""Build cut-context as a standalone Claude skill, for skill marketplaces and for
people who don't use plugins.

    python3 package_skill.py        # writes dist/cut-context/ and dist/cut-context.zip

The skill is built from the plugin's own files, so there is one source of truth.
The only change is its name: as a plugin it runs as /cut-context:audit, as a
standalone skill it runs as /cut-context.
"""

import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "plugins", "cut-context", "skills", "audit")
DIST = os.path.join(HERE, "dist")
NAME = "cut-context"


def main():
    out = os.path.join(DIST, NAME)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    with open(os.path.join(SRC, "SKILL.md")) as fh:
        skill, n = re.subn(r"(?m)^name: audit$", "name: " + NAME, fh.read(), count=1)
    if n != 1:
        print("SKILL.md has no 'name: audit' line", file=sys.stderr)
        return 1
    skill = skill.replace("# cut-context: audit", "# cut-context", 1)
    with open(os.path.join(out, "SKILL.md"), "w") as fh:
        fh.write(skill)
    shutil.copy2(os.path.join(SRC, "audit.py"), out)
    shutil.copy2(os.path.join(HERE, "LICENSE"), out)
    zpath = os.path.join(DIST, NAME + ".zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(os.listdir(out)):
            z.write(os.path.join(out, f), os.path.join(NAME, f))
    print("wrote %s and %s" % (os.path.relpath(out, HERE), os.path.relpath(zpath, HERE)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
