# Third-party notices

Each bundled component remains subject to its upstream license.

| Component | Project | Primary license |
| --- | --- | --- |
| Debian | [debian.org](https://www.debian.org/) | Package-specific |
| CPython | [python.org](https://www.python.org/) | PSF License |
| OpenJDK | [openjdk.org](https://openjdk.org/) | GPL-2.0 with Classpath Exception |
| Node.js | [nodejs.org](https://nodejs.org/) | MIT |
| npm CLI | [github.com/npm/cli](https://github.com/npm/cli) | Artistic-2.0 |
| Go | [go.dev](https://go.dev/) | BSD-3-Clause |
| Apache Maven | [maven.apache.org](https://maven.apache.org/) | Apache-2.0 |
| NumPy | [numpy.org](https://numpy.org/) | BSD-3-Clause |
| pandas | [pandas.pydata.org](https://pandas.pydata.org/) | BSD-3-Clause |
| Matplotlib | [matplotlib.org](https://matplotlib.org/) | Matplotlib License |
| Requests | [requests.readthedocs.io](https://requests.readthedocs.io/) | Apache-2.0 |
| Playwright | [playwright.dev](https://playwright.dev/) | Apache-2.0 |
| Chromium | [chromium.org](https://www.chromium.org/) | BSD-style and bundled component licenses |
| greenlet | [greenlet.readthedocs.io](https://greenlet.readthedocs.io/) | MIT and PSF License |
| pyee | [pyee.readthedocs.io](https://pyee.readthedocs.io/) | MIT |
| Beautiful Soup | [crummy.com/software/BeautifulSoup](https://www.crummy.com/software/BeautifulSoup/) | MIT |
| OpenSandbox Server | [github.com/alibaba/OpenSandbox](https://github.com/alibaba/OpenSandbox) | Apache-2.0 |
| OpenSandbox execd | [github.com/alibaba/OpenSandbox](https://github.com/alibaba/OpenSandbox) | Apache-2.0 |
| bubblewrap | [github.com/containers/bubblewrap](https://github.com/containers/bubblewrap) | LGPL-2.0-or-later |

The separate OpenSandbox Server image retains its upstream distribution and
licenses. Its Docker provisioning source is modified by
`opensandbox-server/apply-patch.py` to provision private-network native sessions
and authenticate access to their privileged parent.

The separate OpenSandbox Execd image builds the upstream source at commit
`4a9db411879601610843af9c8e03563694325b2a` with the modifications in
`opensandbox-execd/patches/`. These modifications provide session ownership,
confirmed cleanup, confined file operations and parent-token protection.
The image retains the upstream bubblewrap binary, native session gate and
license notices. Its source archive, build image and runtime image are pinned
in `opensandbox-execd/Dockerfile`.

Release images include a platform-specific SBOM attestation. Package copyright
files are available under `/usr/share/doc/*/copyright` in the image.

Chromium bundles third-party components with their own license notices. Browser
artifacts and notices are installed under `/opt/sandbox-runtime/browsers`;
Playwright notices are included with the Python package in the runtime environment.
