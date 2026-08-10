# Keep browser verification in coding images

Interactive change requests require behavioral evidence. Downloading Playwright or Chromium inside a time-limited change turn consumes the turn budget, changes the project workspace, and can fail after the implementation is already complete.

## Decision

Pin Playwright and its matching Chromium in both default coding images. Coding turns use that read-only installation and must not install verification-only browser tooling in project workspaces.

## Consequences

Interactive checks can start without a network download. Agent images become larger, and changing the pinned Playwright version requires rebuilding both images. Image-size and build-time changes remain unmeasured.
