The spec above loaded and ran against the live app, and these tests FAILED, or were skipped on an
environment variable nobody defines: {failing}

For each one you get the error, the failing line, and the page's accessibility snapshot at the
moment it failed. The snapshot is the truth about the page: real roles, names, option values,
formats and which elements exist. A skipped test has no snapshot; its error says why it never ran.

{failures}

{rejection}Return the COMPLETE corrected `{spec}` in a single ```typescript block. Rules for this repair:

1. Fix how a test FINDS and DRIVES the page: locators, scoping, option values and formats, the
   day or row it targets, waits, test data set-up, cleanup. Take every value from the snapshots.
2. Never change what a test EXPECTS from the acceptance criteria. Do not delete or loosen an
   assertion, swap an expected value for whatever the page shows, or wrap it in try/catch.
   If the snapshot shows the app really behaves differently from the criterion, keep the
   assertion as it is: that failure is a finding for a person, not a spec bug.
3. If a test cannot be made to work because something it needs is not on the page, replace
   its body with `test.fixme(true, '<what is missing, in one sentence>')`. Do not fake a pass.
4. Do not touch tests that are not listed above, except where a shared constant they use
   was wrong.
5. Keep following the skills and the repository conventions: page objects instead of local
   helper functions, every role locator named and scoped.
6. `test.skip(!process.env.X)` is for credentials the repository already declares. Remove any
   skip on a variable you invented for test data (a trip id, a day, a hotel name), including one
   in `beforeEach` or the describe body, and get that data the way the page contract says.

A repair that drops an expectation from a test, or turns a passing test into fixme, is
rejected automatically and you will be asked again.
