# proxbox-api 0.0.24rc1

## Summary

This release candidate carries the complete 0.0.24 change set for validation on
TestPyPI before the final release. It contains no code changes beyond 0.0.24;
only the package version differs. See [proxbox-api 0.0.24](version-0.0.24.md)
for the full list of fixes, compatibility notes, known limitations and upgrade
guidance.

## Validation

Install the candidate from TestPyPI into a clean environment, confirm the
package imports, the application starts, and the mounted operation inventory
matches, then run one node synchronization and one virtual-machine
synchronization against a non-production NetBox before promoting the final
release.
