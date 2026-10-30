# Project maintenance

Hephestus is published primarily as a cybersecurity portfolio and learning project.
It originated as an MSc research beta, was expanded into V1 for the author's
red-team work and then into V2 for public release. The dissertation evaluated
only the beta; see [research background](docs/RESEARCH-BACKGROUND.md).
There is no promise of active support, feature delivery or pull-request review.
You are welcome to study or fork it under the MIT license. Before investing in a
large contribution, open a small issue describing the problem and proposed scope.

For a focused change, keep local/remote approvals, loopback UI authentication,
credential separation, expiry/cancellation and evidence integrity intact. Include
a safe regression test and document changed behavior. Never attach real engagement
data, credentials, personal or employer information, or model weights.

## Verification

Verify changes locally before submitting them. Check application startup and the
affected workflow using harmless commands on systems you are authorized to use.
Remote workflow changes should also be checked on an authorized Windows target,
including target-side approval, cancellation and disconnection.

Keep model download instructions consistent across [README.md](README.md#models-and-hardware),
[models/README.md](models/README.md) and the in-app Help in
`hephestus/web/static/index.html`. Document supported GGUF/runtime requirements and
keep downloaded weights and verification artifacts out of the repository.

Report sensitive problems using [SECURITY.md](SECURITY.md). Pull requests should
describe the trigger, changed behavior and verification performed.
