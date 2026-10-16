# JFrog Curation Traffic Test

Sends about 100 packages each for **npm, PyPI and Maven** through your Curation-enabled Artifactory repositories
and logs what happened to each one: **allowed**, **blocked** (and by which policy), or served an **older compliant
version** (Compliant Version Selection).

## Run it

You need Python 3.8+ (nothing to install), plus `npm` and/or `pip` for those ecosystems.

```bash
git clone https://github.com/dylanmo-jfrog/curation-traffic-test.git
cd curation-traffic-test

export JF_HOST=yourcompany.jfrog.io        # your JFrog host, no https://
export JF_USER=you@yourcompany.com
export JF_TOKEN=<access token>             # needs read access to the repos below
export NPM_REPO=<your curated npm repo>
export PYPI_REPO=<your curated PyPI repo>
export MAVEN_REPO=<your curated Maven repo>

python3 curation_test.py
```

Takes about 10-15 minutes. To try it quickly first: `python3 curation_test.py --limit 10`.
To run one ecosystem: `python3 curation_test.py npm`.

## Results

Written to `results/<timestamp>/`:

- `summary.txt`: what was blocked (with the policy) and where an older version was served
- `results.csv`: one row per package
- `raw/`: full output per package

| Outcome | Meaning |
|---|---|
| `OK` | Downloaded |
| `BLOCKED` | Curation blocked it; `policy` and `detail` say why |
| `NO_MATCH` | No such package or version |
| `ERROR` | Timeout or other failure |

`older_than_newest = yes` means Curation served an older version than the newest one available in your range.
See **Curation > Audit Events** in JFrog for the full audit trail.

## Notes

- Results depend on **your** Curation policies.
- Blocked packages are never downloaded. Some old PyPI packages run build code when pip fetches their metadata, so
  use a disposable machine or CI runner.
- Your own npm, pip and Maven settings are never touched.
- Edit `packages/*.txt` to test your own packages (one per line: `name version-range`).
- Version-substitution examples (`# CVS:` lines) are time-sensitive. To refresh them for your tenant, run
  `python3 tools/discover_cvs.py npm` (also `pypi`, `maven`) and paste the output into `packages/`.

[JFrog Curation docs](https://docs.jfrog.com/security/docs/curation-intro). Provided as-is for evaluation, not an
official JFrog product.
