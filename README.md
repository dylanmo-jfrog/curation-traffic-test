# JFrog Curation Traffic Test

A small, self-contained test harness that sends realistic package-manager traffic (**npm, PyPI and Maven**)
through your **JFrog Curation**-enabled Artifactory repositories and records what Curation did with each request.

Use it to see, with your own policies and your own tenant:

- which packages are **allowed**,
- which are **blocked**, and by which policy (malicious package, critical CVE, EPSS, immaturity, license, ...),
- where **Compliant Version Selection (CVS)** quietly served an older, compliant version instead of failing a
  version-range install.

About 100 packages per ecosystem are included. Most use version ranges (`^4.0.0`, `>=2.28,<3`, `[32.0,33.0)`) the
way real developers and CI jobs declare dependencies, with a smaller set of pinned vulnerable or malicious versions.

> Outcomes depend on the Curation policies configured on your tenant. The notes in the package lists describe what
> each line is *meant* to exercise; nothing is asserted pass/fail.

## Requirements

| | |
|---|---|
| Python | 3.8+ (standard library only, nothing to `pip install`) |
| npm / pip | On your `PATH` for those ecosystems. Maven is tested over HTTP, so no `mvn` is needed. |
| JFrog | Curation-enabled **remote or virtual** repositories for each ecosystem, and an access token with read access to them |
| Network | Reachability to your JFrog platform. Optional: direct internet access for a read-only lookup of the newest public version (used to detect version substitution; skip with `--no-upstream-check`) |

## Quick start

```bash
git clone https://github.com/dylanmo-jfrog/curation-traffic-test.git
cd curation-traffic-test

export JF_HOST=yourcompany.jfrog.io          # your JFrog platform host (no https://)
export JF_USER=you@yourcompany.com           # Artifactory user
export JF_TOKEN=<access token>               # read access to the repositories below
export NPM_REPO=npm-curated-virtual          # your Curation-enabled repositories (virtual or remote)
export PYPI_REPO=pypi-curated-virtual
export MAVEN_REPO=maven-curated-virtual

python3 curation_test.py --limit 10          # quick smoke test (10 packages per ecosystem)
python3 curation_test.py                     # full run, roughly 5-10 minutes
python3 curation_test.py npm                 # a single ecosystem
```

Ecosystems whose repository variable is not set, or whose client is not installed, are skipped with a message.

Options: `--workers N` (parallelism, default 4), `--timeout SEC` (per package, default 300), `--limit N`,
`--no-upstream-check`.

## Reading the results

Each run writes to `results/<timestamp>/`:

| File | Contents |
|---|---|
| `summary.txt` | Counts per ecosystem, every blocked package with the policy that blocked it, every version substitution |
| `results.csv`, `results.jsonl` | One row per package (see columns below) |
| `raw/<ecosystem>/*.log` | Full client output for every package, with credentials removed |

**Outcomes**

| Outcome | Meaning |
|---|---|
| `OK` | The package (or a compliant version of it) was resolved and downloaded |
| `BLOCKED` | Curation returned 403. `policy` and `detail` show why (the blocked package can be a transitive dependency) |
| `NO_MATCH` | No matching version or package was found (for example a version that no longer exists upstream) |
| `ERROR` | Timeout or another failure unrelated to a policy decision |

**Useful CSV columns:** `resolved_version`, `newest_public_in_range`, `older_than_newest`, `blocked_package`, `policy`, `detail`.
`older_than_newest = yes` means Curation served an older version than the newest public one in your requested
range, i.e. Compliant Version Selection (or a not-yet-offered new release) kicked in.

For the full audit trail of every decision, see **Curation > Audit Events** in the JFrog Platform.

## Customizing the package lists

Edit `packages/npm.txt`, `packages/pypi.txt` and `packages/maven.txt`. One package per line:

```
# name   version-spec   # optional note
lodash                ^4.0.0
requests              >=2.28,<3
com.google.guava:guava  [32.0,33.0)
```

- npm uses semver ranges, PyPI uses pip specifiers, Maven uses version-range syntax (or a plain version to pin).
- Add your own internal-approved and internal-forbidden packages to see how your policies treat them.

## Refreshing the Compliant Version Selection cases

CVS only appears when the newest version in a range is blocked or not yet offered while an older version in the
same range is allowed. For example, a brand-new release held back by an immaturity policy, or a fresh CVE.
Those candidates change daily, so `tools/discover_cvs.py` finds them against **your** tenant and policies:

```bash
python3 tools/discover_cvs.py npm   --max 400 > cvs_npm.txt      # same environment variables as above
python3 tools/discover_cvs.py pypi  --max 300 > cvs_pypi.txt
python3 tools/discover_cvs.py maven > cvs_maven.txt              # uses tools/maven_pool.txt, or --pool FILE
```

Paste the lines you want into the matching `packages/*.txt`. A scan of 300-400 packages takes roughly 5-10 minutes.
The `# CVS:` ranges shipped in the lists were discovered on one test tenant on a single day and will go stale
within about a week, so refresh them before a demo or evaluation.

## How it works

- **npm** and **PyPI** use the real `npm` and `pip` clients, so version ranges resolve exactly as they do for your
  developers. Each package runs in a throwaway temp directory with a generated config; your own `.npmrc`, pip
  configuration and caches are never touched.
- **Maven** is exercised at the repository protocol level (metadata, version-range resolution, `.pom`, `.jar`),
  the same requests `mvn` makes, without running Maven's plugin tree through Curation.
- Credentials are passed to the clients only for the duration of each run and are stripped from the saved logs.

## Safety notes

- Blocked packages are never downloaded. Allowed packages are downloaded into temporary directories that are deleted
  afterwards. npm installs run with `--ignore-scripts`.
- For some older PyPI source distributions, `pip download` runs the package's metadata build step, which executes
  package-supplied code. Run the suite on a disposable machine, container or CI runner rather than a workstation
  with sensitive credentials.
- The lists intentionally contain packages with known vulnerabilities and JFrog's inert malicious-package test
  packages. They are requested through Curation, which is the point of the test. Do not use them as real dependencies.
- Keep your access token out of shell history and CI logs where possible.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `Missing environment variables` | Set `JF_HOST`, `JF_USER` and `JF_TOKEN` |
| Everything `BLOCKED` or HTTP 401/403 with no policy text | Token lacks read access to the repositories, or the repository name is wrong |
| Many `ERROR` rows with `TIMEOUT` | Slow network or heavy npm dependency trees; raise `--timeout` or lower `--workers` |
| `older_than_newest` is always blank | The public-version lookup could not reach npmjs.org / pypi.org / Maven Central; this does not affect pass/fail of the pulls |
| Few or no version substitutions | Your policies may not currently hold back any newest releases; run `tools/discover_cvs.py` to find ranges that do |

## Learn more

- [JFrog Curation documentation](https://docs.jfrog.com/security/docs/curation-intro)

## Disclaimer

This is a community-style sample provided as-is for evaluation and demonstration. It is not an official JFrog
product and is not covered by JFrog support.
