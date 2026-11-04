# Forks

Polaris builds on two upstream projects, maintained as separate fork
repositories — referenced here as git submodules, never vendored.

| Path | Upstream | Fork repo | License |
|---|---|---|---|
| `forks/chatwoot` | [chatwoot/chatwoot](https://github.com/chatwoot/chatwoot) | [Jiun01/polaris-chatwoot](https://github.com/Jiun01/polaris-chatwoot) | MIT |
| `forks/easyappointments` | [alextselegidis/easyappointments](https://github.com/alextselegidis/easyappointments) | [Jiun01/polaris-ea-fork](https://github.com/Jiun01/polaris-ea-fork) | GPL-3.0 |

Each fork's own license applies to its own tree — see the LICENSE file inside
each repository. Nothing in the parent repo's all-rights-reserved license
extends over upstream code.

## Community Edition note

`polaris-chatwoot` publishes the upstream **Community Edition** tree only:
Chatwoot's proprietary `enterprise/` overlay is stripped from the public
history (mirroring upstream's own FOSS procedure — `rm -rf enterprise
spec/enterprise`). The private deployment builds from a private checkout that
retains the enterprise overlay; this public tree is the community-edition
surface and every `ChatwootApp.enterprise?` check resolves false here.

## Build contexts

`infra/docker-bake.hcl` / `infra/ship.sh` build fork images from the
operator's sibling checkouts (`../polaris-cw-fork`, `../polaris-ea-fork`),
not from these submodules — the deploy pipeline pins specific private fork
revisions. These submodules are the public, readable reference copies.
