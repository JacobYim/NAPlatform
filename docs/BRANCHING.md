# Branching Policy

NAPlatform uses a stable-main, integration-dev, phase-branch workflow.

## Branch roles

- `main`: GitHub default branch and stable release baseline. Do not update `main` after each phase.
- `dev`: integration branch for completed phases.
- `phase/*`: one branch per implementation phase or update.

## Merge flow

```text
phase/N-*  -> dev
phase/N+1-* -> dev
...
dev        -> main only after all planned phases for the release are complete
```

## Operational rules

1. Create each phase from the latest `dev`.
2. Commit and push the phase branch.
3. Merge the phase branch into `dev` after tests/review pass.
4. Push `dev`.
5. Do **not** push `dev` to `main` until the full phase set is complete and ready for a stable release.
6. Keep GitHub's default branch set to `main`.

This repository's GitHub default branch has been corrected to `main`.
