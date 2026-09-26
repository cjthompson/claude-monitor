# Current-run idle hand-off design

Automatic idle capture considers only sessions that deliver non-replayed hook events while the monitor is running. It waits the configured idle interval after the latest such event, captures once for that activity period, and stops after SessionEnd or removal of the session's panel. Closed metadata is discarded by the poller, with a one-minute grace period for a newly arriving event to mount its panel. Restarting the monitor starts a new observation period.

The monitor must not replay the complete event log into idle-capture state. The existing recent-event replay may still restore the visible dashboard, but replayed events never enroll sessions for automatic capture. Automatic rotation follows the same current-run observation rule. Durable pending delivery remains unchanged.

Manual capture, SessionEnd capture, saved hand-offs, and CLI selection of an explicit older session remain available. Replay metadata can resolve manual capture without enrolling idle capture. When an open pane's session is older than the recent replay window, manual capture searches the event log only on demand for that pane. A fresh hook can restore an exact durable pending rotation. The separate transcript-picker change adds an on-demand UI path for historical transcripts.

Verify with tests that historical events never trigger capture, a current-run session captures after its idle interval exactly once, resumed activity rearms it, and ended or removed sessions do not capture.
