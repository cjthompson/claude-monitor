# iTerm2 Session Note API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a supported iTerm2 Python API for reading and updating a terminal session's Session Note so claude-monitor can keep an actionable, persistent hand-off visible above a full-screen alternate-screen application without injecting it into Claude.

**Architecture:** Reuse iTerm2's existing generic session-property RPC with one JSON property named `session_note`; document the property in the existing protobuf comments but do not add a message or change the protobuf schema. Put strict parsing and state transitions beside the existing Session Note model, expose only the small view operation needed to apply collapsed state without taking focus, and wrap the property in typed Python SDK methods. The API is a patch API: omitted fields preserve their current value. Showing a note must use the existing restore path so API updates never focus the note or expand it unexpectedly.

**Tech Stack:** Objective-C and Swift in iTerm2, XCTest/ModernTests, iTerm2's Python SDK, pytest, Sphinx/reStructuredText, `make run` with an isolated iTerm2 suite.

---

## Product contract

This plan implements the terminal-surface capability only. It does not add
claude-monitor capture, summarization, rotation, or delivery code.

The motivating consumer will maintain one current hand-off per iTerm2 session.
That note may be updated while Claude is occupying the alternate screen. It must
remain local until the user explicitly chooses a separate “Send to Claude”
action.

The public Python surface is:

```python
note = await session.async_get_session_note()

await session.async_set_session_note(
    text="GWTH-23546 — review authorization failures\nNext: run the focused spec",
    visible=True,
    collapsed=False,
)
```

`async_get_session_note()` returns a `SessionNote` value with three read-only
properties:

- `text: str`
- `visible: bool`
- `collapsed: bool`

`async_set_session_note()` accepts any non-empty subset of those fields. `None`
means “do not change this field.” It returns `None`; callers read the resulting
state with `async_get_session_note()` when they need confirmation.

The wire property is named `session_note`.

GET always returns all three fields. A session with no note returns:

```json
{"text":"","visible":false,"collapsed":false}
```

The existing user action can briefly create a visible, empty editor while the
user is composing a brand-new note. GET may report that transient UI state as
empty and visible. The API must not create that state, and any API update that
leaves it empty and does not request a visible/collapsed state canonicalizes it
back to no note.

SET accepts a JSON object with a non-empty subset of `text`, `visible`, and
`collapsed`:

```json
{"text":"current hand-off","visible":true,"collapsed":false}
```

Rules:

1. Validate the complete object before mutating anything.
2. Reject an empty object, unknown keys, JSON `null`, and wrong value types with
   `INVALID_VALUE`.
3. Omitted fields preserve their current values.
4. A non-empty `text` creates a model if needed. If `visible` is omitted on a
   new note, the note is created hidden.
5. `visible: true` requires the resulting text to be non-empty and a live
   `SessionView`; otherwise return `INVALID_VALUE` for empty text or
   `IMPOSSIBLE` when there is no view.
6. `visible: false` hides the floating note but preserves its text and collapsed
   state.
7. `collapsed` may be changed for a hidden note and takes effect when the note
   is shown. `collapsed: true` is invalid when the resulting text is empty.
8. Setting `text` to the empty string clears the note: hide the view, discard
   the model, and report the canonical empty state, even when the old note was
   visible or collapsed. The same patch cannot explicitly request
   `visible: true` or `collapsed: true`.
9. Showing or updating a note never steals keyboard focus. Only the existing
   user-invoked Edit Session Note action focuses it.
10. Browser or buried sessions may retain hidden note text, but cannot be made
    visible without a live `SessionView`.

This is one independently shippable upstream PR. The app handler without an SDK
surface, or the SDK surface without app support, would not be a complete feature.

## Task 1: Clone upstream and create the isolated contributor worktree

**Repository:** <https://github.com/gnachman/iTerm2>

The repository's default branch is `master`. This plan was checked against
upstream commit `cee9cc21d76cd5dbb15e809193f82b4dc7df443f`; refresh `master`
before implementation because upstream may have moved.

**Files:**

- Read: `AGENTS.md`
- Read: `CLAUDE.md`
- Local-only edit if necessary: `.git/info/exclude`

- [ ] **Step 1: Clone upstream with submodules**

```bash
mkdir -p ~/workspace
git clone --recursive https://github.com/gnachman/iTerm2.git ~/workspace/iTerm2
cd ~/workspace/iTerm2
git remote -v
git status --short --branch
```

Expected: `origin` points to `gnachman/iTerm2`, the current branch is
`master`, and the checkout is clean.

- [ ] **Step 2: Read the repository-specific instructions in full**

```bash
cat AGENTS.md
cat CLAUDE.md
cat docs/release-notes-guidelines.md
```

Key constraints to preserve: warnings are errors, existing Objective-C files
remain Objective-C, new classes are Swift, `make run` supplies an isolated
suite, ModernTests run through `tools/run_tests.expect`, and generated plans
must not be committed to iTerm2.

- [ ] **Step 3: Create the required local worktree path and branch**

First check whether the worktree directory is already ignored:

```bash
git check-ignore -q .Codex/worktrees/
```

If that exits non-zero, use `apply_patch` to add this local-only line to
`.git/info/exclude`:

```text
.Codex/worktrees/
```

Then create the worktree:

```bash
git fetch origin master
git worktree add -b ct/session-note-api .Codex/worktrees/ct/session-note-api origin/master
cd .Codex/worktrees/ct/session-note-api
git status --short --branch
git submodule update --init --recursive
```

Expected: the branch is `ct/session-note-api`, the worktree is clean, and all
submodules are initialized.

- [ ] **Step 4: Verify the contributor toolchain**

```bash
make doctor
/opt/homebrew/opt/protobuf@21/bin/protoc --version
```

Expected: the repository doctor passes and protoc reports a supported 3.19,
3.20, or 3.21 version. If the doctor names missing dependencies, run the
interactive `make setup`, review each requested installation, and rerun the
doctor. Do not use `make dangerous-setup`.

- [ ] **Step 5: Record the baseline before editing**

```bash
git rev-parse HEAD
rg -n "setPropertyInSession|getPropertyFromSession" sources/API/iTermAPIHelper.m
rg -n "sessionNoteModel|restoreSessionNoteWithModel|isSessionNoteVisible" sources/PTYSession sources/TerminalView
```

If the named APIs no longer exist, stop and update this plan from the current
code instead of forcing the old file map onto a changed implementation.

## Task 2: Specify and test strict Session Note patch parsing

**Files:**

- Modify: `sources/SessionNotes/SessionNoteModel.swift`
- Create: `ModernTests/SessionNoteAPITests.swift`
- Modify: `iTerm2.xcodeproj/project.pbxproj` (with the repository helper)

Keep the parsing type in `SessionNoteModel.swift`; introducing a separate Swift
source file would add needless Xcode project-file churn for a small value type.

- [ ] **Step 1: Add failing parser tests**

Create `ModernTests/SessionNoteAPITests.swift` with `XCTest` and
`@testable import iTerm2SharedARC`. Cover these cases:

```swift
final class SessionNoteAPITests: XCTestCase {
    func testParseAcceptsPartialPatch() {
        let update = SessionNoteAPIUpdate.parse([
            "text": "next: run tests",
            "visible": true,
        ])
        XCTAssertEqual(update?.text, "next: run tests")
        XCTAssertEqual(update?.visible, true)
        XCTAssertNil(update?.collapsed)
    }

    func testParseRejectsEmptyObject() {
        XCTAssertNil(SessionNoteAPIUpdate.parse([:]))
    }

    func testParseRejectsUnknownKey() {
        XCTAssertNil(SessionNoteAPIUpdate.parse(["title": "wrong"]))
    }

    func testParseRejectsWrongTypesAndNull() {
        XCTAssertNil(SessionNoteAPIUpdate.parse(["text": 1]))
        XCTAssertNil(SessionNoteAPIUpdate.parse(["visible": 1]))
        XCTAssertNil(SessionNoteAPIUpdate.parse(["collapsed": NSNull()]))
    }
}
```

Use strict JSON booleans. Numeric `0` and `1` must not be accepted as booleans.
Immediately add the new file to git and the ModernTests target, as required by
the upstream instructions:

```bash
git add ModernTests/SessionNoteAPITests.swift
tools/add_file_to_xcodeproj.rb ModernTests/SessionNoteAPITests.swift ModernTests
git add iTerm2.xcodeproj/project.pbxproj
```

- [ ] **Step 2: Run the focused test and confirm it fails for the missing type**

```bash
tools/run_tests.expect ModernTests/SessionNoteAPITests
```

Expected: compilation fails because `SessionNoteAPIUpdate` does not exist.

- [ ] **Step 3: Implement the parser beside the model**

Add a Swift value type exposed to Objective-C. The implementation should have
optional Swift fields for easy state merging and explicit Objective-C accessors
(`hasText`, `textValue`, `hasVisible`, `visibleValue`, `hasCollapsed`,
`collapsedValue`) so `iTermAPIHelper.m` never has to interpret Swift optionals.

Use this shape:

```swift
@objc(iTermSessionNoteAPIUpdate)
final class SessionNoteAPIUpdate: NSObject {
    let text: String?
    let visible: Bool?
    let collapsed: Bool?

    @objc var hasText: Bool { text != nil }
    @objc var textValue: String { text ?? "" }
    @objc var hasVisible: Bool { visible != nil }
    @objc var visibleValue: Bool { visible ?? false }
    @objc var hasCollapsed: Bool { collapsed != nil }
    @objc var collapsedValue: Bool { collapsed ?? false }

    private init(text: String?, visible: Bool?, collapsed: Bool?) {
        self.text = text
        self.visible = visible
        self.collapsed = collapsed
    }

    @objc(parse:)
    static func parse(_ object: Any) -> SessionNoteAPIUpdate? {
        guard let dictionary = object as? NSDictionary,
              dictionary.count > 0 else {
            return nil
        }
        let allowed = Set(["text", "visible", "collapsed"])
        let keys = dictionary.allKeys.compactMap { $0 as? String }
        guard keys.count == dictionary.count,
              Set(keys).isSubset(of: allowed) else {
            return nil
        }

        var text: String?
        if let value = dictionary["text"] {
            guard let string = value as? String else { return nil }
            text = string
        }

        func strictBoolean(_ key: String) -> Bool?? {
            guard let value = dictionary[key] else { return .some(nil) }
            guard CFGetTypeID(value as CFTypeRef) == CFBooleanGetTypeID(),
                  let number = value as? NSNumber else {
                return nil
            }
            return .some(number.boolValue)
        }

        guard let visible = strictBoolean("visible"),
              let collapsed = strictBoolean("collapsed") else {
            return nil
        }
        return SessionNoteAPIUpdate(
            text: text,
            visible: visible,
            collapsed: collapsed)
    }
}
```

Import `CoreFoundation` if the compiler does not make the `CFGetTypeID` symbols
available through Foundation. Do not loosen the parser to truthy values.

- [ ] **Step 4: Run the parser tests**

```bash
tools/run_tests.expect ModernTests/SessionNoteAPITests
```

Expected: the parser tests pass.

- [ ] **Step 5: Commit the parsing unit**

```bash
git add sources/SessionNotes/SessionNoteModel.swift ModernTests/SessionNoteAPITests.swift
git commit -m "Add strict Session Note API patch parsing"
```

## Task 3: Implement and test Session Note state transitions

**Files:**

- Modify: `sources/PTYSession/PTYSession.swift`
- Modify: `sources/TerminalView/SessionView.h`
- Modify: `sources/TerminalView/SessionView.m`
- Modify: `ModernTests/SessionNoteAPITests.swift`

The state-transition code belongs on `PTYSession` because it owns both the
persisted model and the live `SessionView`. It must be callable from Objective-C
by the generic property handler.

- [ ] **Step 1: Add failing model-level transition tests**

Extend `SessionNoteAPITests` with a fresh synthetic session per test. Test the
headless/model behavior without constructing a window:

```swift
private func makeSession() -> PTYSession {
    PTYSession(synthetic: false)!
}

func testEmptySessionHasCanonicalSnapshot() {
    let session = makeSession()
    let snapshot = session.sessionNoteAPIDictionary
    XCTAssertEqual(snapshot["text"] as? String, "")
    XCTAssertEqual(snapshot["visible"] as? Bool, false)
    XCTAssertEqual(snapshot["collapsed"] as? Bool, false)
}

func testTextCreatesHiddenNoteAndPartialPatchPreservesText() {
    let session = makeSession()
    XCTAssertTrue(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["text": "next action"])!))
    XCTAssertEqual(session.sessionNoteModel?.text, "next action")
    XCTAssertFalse(session.sessionNoteAPIDictionary["visible"] as! Bool)

    XCTAssertTrue(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["collapsed": true])!))
    XCTAssertEqual(session.sessionNoteModel?.text, "next action")
    XCTAssertTrue(session.sessionNoteModel!.isCollapsed)
}

func testEmptyTextClearsModel() {
    let session = makeSession()
    XCTAssertTrue(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["text": "temporary"])!))
    XCTAssertTrue(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["collapsed": true])!))
    XCTAssertTrue(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["text": ""])!))
    XCTAssertNil(session.sessionNoteModel)
}

func testRejectsVisibleOrCollapsedEmptyNoteWithoutMutation() {
    let session = makeSession()
    XCTAssertFalse(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["visible": true])!))
    XCTAssertFalse(session.applySessionNoteAPIUpdate(
        SessionNoteAPIUpdate.parse(["collapsed": true])!))
    XCTAssertNil(session.sessionNoteModel)
}
```

Also test that `{"text":"","visible":true}` and
`{"text":"","collapsed":true}` fail without clearing an existing note.
That proves semantic validation occurs before mutation.

- [ ] **Step 2: Run the focused test and confirm the new tests fail**

```bash
tools/run_tests.expect ModernTests/SessionNoteAPITests
```

Expected: compilation fails because the `PTYSession` API methods do not exist.

- [ ] **Step 3: Add a non-focusing collapsed-state operation to SessionView**

Add this declaration to the Session Note section of `SessionView.h`:

```objc
- (void)setSessionNoteCollapsed:(BOOL)collapsed;
```

Implement it in `SessionView.m` beside `restoreSessionNoteWithModel:`:

```objc
- (void)setSessionNoteCollapsed:(BOOL)collapsed {
    _sessionNoteView.isCollapsed = collapsed;
}
```

This deliberately does not create a view and does not focus anything. The
caller first creates/restores the view if it needs one.

- [ ] **Step 4: Add snapshot and apply methods to PTYSession.swift**

Place these beside the existing `textViewEditSessionNote()` implementation.
Expose stable Objective-C selectors with `@objc`.

The snapshot must be equivalent to:

```swift
@objc(sessionNoteAPIDictionary)
var sessionNoteAPIDictionary: NSDictionary {
    let model = sessionNoteModel
    return [
        "text": model?.text ?? "",
        "visible": view?.isSessionNoteVisible ?? false,
        "collapsed": model?.isCollapsed ?? false,
    ]
}
```

The apply method must:

1. If the patch explicitly sets empty text, first reject an explicit
   `visible: true` or `collapsed: true`; otherwise hide the view, set
   `sessionNoteModel = nil`, and return success. Do not merge old visibility or
   collapse state into a clear operation.
2. For every other patch, calculate `nextText`, `nextVisible`, and
   `nextCollapsed` from the current snapshot plus the patch.
3. Reject an empty resulting note that requests visible or collapsed state
   before changing the model or view. If the result is empty without either
   request, hide the view, discard the model, and return success.
4. Reject `nextVisible == true` when `view == nil` before changing anything.
5. Otherwise create/reuse the model and update its text.
6. Apply explicit visibility through `restoreSessionNote(with:)` or
   `hideSessionNote()`. Never call `showSessionNote(with:)`, because that path
   intentionally expands and focuses the note.
7. If collapse was explicit and the view is visible, call
   `setSessionNoteCollapsed`; otherwise write `model.isCollapsed` directly.

Use this signature:

```swift
@objc(applySessionNoteAPIUpdate:)
func applySessionNoteAPIUpdate(_ update: SessionNoteAPIUpdate) -> Bool
```

The `Bool` reports semantic success. The API handler separately maps a missing
live view for `visible: true` to `IMPOSSIBLE`.

- [ ] **Step 5: Run the focused transition tests**

```bash
tools/run_tests.expect ModernTests/SessionNoteAPITests
```

Expected: all parser and transition tests pass.

- [ ] **Step 6: Commit the state-transition unit**

```bash
git add sources/PTYSession/PTYSession.swift sources/TerminalView/SessionView.h sources/TerminalView/SessionView.m ModernTests/SessionNoteAPITests.swift
git commit -m "Apply Session Note updates without stealing focus"
```

## Task 4: Wire the `session_note` generic property into iTerm2

**Files:**

- Modify: `proto/api.proto` (property-contract comments only)
- Modify: `sources/API/iTermAPIHelper.m`
- Modify: `ModernTests/SessionNoteAPITests.swift`

The existing `SetPropertyRequest` and `GetPropertyRequest` already address a
session by ID and carry a JSON value, so do not add a new protobuf message,
field, or status. Update the property-list comments in `proto/api.proto`, then
run the required generator and retain only deterministic output produced by
that comment change.

- [ ] **Step 1: Document the property in the existing wire contract**

Add `session_note` to the session-property comments above both
`GetPropertyRequest.name` and `SetPropertyRequest.name`:

```proto
// "session_note" -> { "text": string, "visible": boolean, "collapsed": boolean }
```

For SET, add a second comment line explaining that it accepts a partial object
and omitted keys retain their values. Do not alter field numbers or messages.
Regenerate exactly as upstream requires:

```bash
tools/build_proto.sh
git diff -- proto/api.proto sources/proto/Api.pbobjc.h sources/proto/Api.pbobjc.m api/library/python/iterm2/iterm2/api_pb2.py api/library/python/iterm2/iterm2/api_pb2.pyi
```

Expected: `proto/api.proto` changes and generated files may have comment-only
changes. Any descriptor, field-number, enum, or runtime-version change means
the wrong protoc/toolchain was used; revert those generated changes and fix the
toolchain before continuing.

- [ ] **Step 2: Add final contract cases to the focused tests**

Add assertions for:

- Snapshot reflects model text and collapsed state.
- Hiding preserves model text.
- Updating text preserves collapsed state.
- A clear returns the canonical empty snapshot.
- A visible request against a synthetic session with no view fails atomically.

These tests exercise the methods the Objective-C handler delegates to. The
end-to-end RPC route will be covered against the custom app in Task 7.

- [ ] **Step 3: Add the setter handler**

Add `#import "iTerm2SharedARC-Swift.h"` with the imports in
`iTermAPIHelper.m`; the handler needs the generated declarations for the new
Swift parser and `PTYSession` methods. Then, in
`setPropertyInSession:name:value:`, add a `setSessionNote` block. Its logic must
be:

```objc
SetSessionPropertyBlock setSessionNote = ^ITMSetPropertyResponse_Status {
    iTermSessionNoteAPIUpdate *update =
        [iTermSessionNoteAPIUpdate parse:value];
    if (!update) {
        return ITMSetPropertyResponse_Status_InvalidValue;
    }
    NSString *resultingText = update.hasText
        ? update.textValue
        : (session.sessionNoteModel.text ?: @"");
    if (resultingText.length == 0 &&
        ((update.hasVisible && update.visibleValue) ||
         (update.hasCollapsed && update.collapsedValue))) {
        return ITMSetPropertyResponse_Status_InvalidValue;
    }
    if (update.hasVisible && update.visibleValue && !session.view) {
        return ITMSetPropertyResponse_Status_Impossible;
    }
    if (![session applySessionNoteAPIUpdate:update]) {
        return ITMSetPropertyResponse_Status_InvalidValue;
    }
    return ITMSetPropertyResponse_Status_Ok;
};
```

Register it in the existing map:

```objc
@{ @"grid_size": setGridSize,
   @"buried": setBuried,
   @"session_note": setSessionNote }
```

- [ ] **Step 4: Add the getter handler**

In `getPropertyFromSession:name:`, add:

```objc
GetSessionPropertyBlock getSessionNote = ^NSString * {
    return [NSJSONSerialization
        it_jsonStringForObject:session.sessionNoteAPIDictionary];
};
```

Register `@"session_note": getSessionNote` in the session-property handler
map. The getter must work for normal, buried, and browser sessions and always
return the canonical three-key object.

- [ ] **Step 5: Build immediately to catch Swift/Objective-C selector issues**

```bash
tools/build.sh Development
```

Expected: the build succeeds with no warnings. If the generated Swift selector
differs, fix the explicit `@objc(...)` spelling rather than using dynamic
dispatch or suppressing the warning.

- [ ] **Step 6: Re-run the focused tests**

```bash
tools/run_tests.expect ModernTests/SessionNoteAPITests
```

Expected: all focused tests pass.

- [ ] **Step 7: Commit the RPC property**

```bash
git add proto/api.proto sources/API/iTermAPIHelper.m ModernTests/SessionNoteAPITests.swift
git add sources/proto/Api.pbobjc.h sources/proto/Api.pbobjc.m api/library/python/iterm2/iterm2/api_pb2.py api/library/python/iterm2/iterm2/api_pb2.pyi
git commit -m "Expose Session Notes as a session property"
```

If a generated path is unchanged, omit it from the second `git add`; do not
stage unrelated generator output.

## Task 5: Add the typed Python SDK surface test-first

**Files:**

- Create: `api/library/python/iterm2/tests/test_session.py`
- Modify: `api/library/python/iterm2/iterm2/session.py`
- Modify: `api/library/python/iterm2/iterm2/__init__.py`

Do not bump `api/library/python/iterm2/iterm2/_version.py`; package-version
changes belong to the maintainer's release process.

- [ ] **Step 1: Add failing SDK tests**

In `test_session.py`, construct a `Session` from a minimal
`SplitTreeNode.SplitTreeLink`, monkeypatch `iterm2.rpc.async_get_property` and
`async_set_property`, and execute coroutines with `asyncio.run` so the test does
not require a new pytest plugin.

Immediately stage the new test file after creating it, as upstream requires for
new files:

```bash
git add api/library/python/iterm2/tests/test_session.py
```

Cover:

1. `SessionNote` exposes `text`, `visible`, and `collapsed`.
2. `async_get_session_note()` requests `session_note` for the correct session
   ID and parses the complete response.
3. `async_set_session_note(text="x")` sends only `{"text":"x"}`.
4. A full patch serializes all three fields with JSON booleans.
5. Calling the setter with all defaults raises `ValueError` before making an
   RPC.
6. Non-OK get and set statuses raise `iterm2.rpc.RPCException`.

The fake response only needs the nested attributes consumed by the methods;
use `types.SimpleNamespace` rather than generated response builders.

- [ ] **Step 2: Run the focused SDK test and confirm it fails**

```bash
cd api/library/python/iterm2
python3 -m pytest tests/test_session.py -v
```

Expected: failures because `SessionNote` and the two methods do not exist.

- [ ] **Step 3: Add the immutable result type**

Add `SessionNote` immediately after `SessionLineInfo` in `session.py`:

```python
class SessionNote:
    """Describes a session's floating Session Note."""

    def __init__(self, text: str, visible: bool, collapsed: bool):
        self.__text = text
        self.__visible = visible
        self.__collapsed = collapsed

    @property
    def text(self) -> str:
        """The plain-text contents of the note."""
        return self.__text

    @property
    def visible(self) -> bool:
        """Whether the note is currently visible over the terminal."""
        return self.__visible

    @property
    def collapsed(self) -> bool:
        """Whether the note is collapsed to its title bar."""
        return self.__collapsed
```

Export it from `iterm2/__init__.py` beside `Session`.

- [ ] **Step 4: Add the Session methods**

Add:

```python
async def async_get_session_note(self) -> SessionNote:
    """Returns this session's Session Note state.

    :throws: :class:`~iterm2.rpc.RPCException` if something goes wrong.
    """
    response = await iterm2.rpc.async_get_property(
        self.connection,
        "session_note",
        session_id=self.session_id)
    status = response.get_property_response.status
    if status != iterm2.api_pb2.GetPropertyResponse.Status.Value("OK"):
        raise iterm2.rpc.RPCException(
            iterm2.api_pb2.GetPropertyResponse.Status.Name(status))
    value = json.loads(response.get_property_response.json_value)
    return SessionNote(
        value["text"], value["visible"], value["collapsed"])

async def async_set_session_note(
        self,
        text: typing.Optional[str] = None,
        visible: typing.Optional[bool] = None,
        collapsed: typing.Optional[bool] = None) -> None:
    """Updates selected fields of this session's Session Note.

    Omitted fields retain their current values. Set ``text`` to an empty
    string to remove the note. Showing a note does not move keyboard focus.

    :param text: New plain-text contents, or ``None`` to preserve them.
    :param visible: Whether the note is shown, or ``None`` to preserve it.
    :param collapsed: Whether the note is collapsed, or ``None`` to preserve it.
    :throws: :class:`ValueError` if no fields are supplied.
    :throws: :class:`~iterm2.rpc.RPCException` if iTerm2 rejects the update.
    """
    value = {}
    if text is not None:
        value["text"] = text
    if visible is not None:
        value["visible"] = visible
    if collapsed is not None:
        value["collapsed"] = collapsed
    if not value:
        raise ValueError("At least one Session Note field is required")
    await self._async_set_property("session_note", json.dumps(value))
```

Keep `typing.Optional` rather than newer union syntax so the SDK retains its
current Python compatibility.

- [ ] **Step 5: Run SDK tests and static checks**

```bash
cd api/library/python/iterm2
python3 -m pytest tests/test_session.py -v
python3 -m pytest tests/ -v
make pylint
cd ../../../..
```

Expected: the focused and complete SDK tests pass, and pylint reports no new
errors. Run the repository's existing mypy target too:

```bash
cd api/library/python/iterm2
make mypy
cd ../../../..
```

Expected: mypy completes without a new error.

- [ ] **Step 6: Commit the SDK surface**

```bash
git add api/library/python/iterm2/iterm2/session.py api/library/python/iterm2/iterm2/__init__.py api/library/python/iterm2/tests/test_session.py
git commit -m "Add Python API for Session Notes"
```

## Task 6: Document the API and user-visible change

**Files:**

- Modify: `api/library/python/iterm2/docs/session.rst`
- Modify: `docs/notes-3.7.txt`

- [ ] **Step 1: Add the new members to the Session API page**

Add `async_get_session_note` and `async_set_session_note` to the explicit
`:members:` list for `iterm2.Session`. Add a value-type section:

```rst
.. autoclass:: iterm2.session.SessionNote
   :members:
```

- [ ] **Step 2: Add a release-note bullet under New Features**

Use wording equivalent to this, wrapped to at most 50 columns:

```text
- The Python API can now read, show, hide, and
  update a session’s Session Note.
```

Run the line-length check required by the upstream guidelines:

```bash
awk 'length($0) > 50 { print NR ":" length($0) ":" $0; bad=1 } END { exit bad }' docs/notes-3.7.txt
```

Expected: no output and exit status 0.

- [ ] **Step 3: Build the Python documentation**

```bash
cd api/library/python/iterm2
make docs
cd ../../../..
```

Expected: Sphinx completes without a missing member or reference warning. The
command may open the built documentation in a browser; inspect the Session page
and confirm both methods and all three `SessionNote` properties render.

- [ ] **Step 4: Commit documentation**

```bash
git add api/library/python/iterm2/docs/session.rst docs/notes-3.7.txt
git commit -m "Document the Session Note API"
```

## Task 7: Build and test a custom iTerm2 end to end

**Files:**

- Create temporarily, do not commit: `/tmp/iterm2-session-note-smoke.py`

The worktree basename is `session-note-api`, so `make run` starts the custom
app with `-suite session-note-api`. This keeps its settings, API socket, and
process identity separate from the installed iTerm2 where this work is being
performed.

- [ ] **Step 1: Build the custom app**

```bash
tools/build.sh Development
```

Expected: build succeeds with no warnings. The log is in `tmp/build.log` if it
fails.

- [ ] **Step 2: Launch the isolated app in a dedicated terminal**

```bash
make run
```

Leave that command running. In the custom app, enable the Python API under
Settings > General > Magic > Enable Python API. Create two panes so background
targeting and focus preservation can be tested.

- [ ] **Step 3: Create an isolated SDK environment**

From a second terminal in the feature worktree:

```bash
python3 -m venv /tmp/iterm2-session-note-sdk
/tmp/iterm2-session-note-sdk/bin/python -m pip install -e api/library/python/iterm2
```

Expected: importing `iterm2` from that interpreter resolves to this worktree's
modified SDK.

- [ ] **Step 4: Create the temporary smoke script with `apply_patch`**

Write `/tmp/iterm2-session-note-smoke.py` with:

```python
#!/usr/bin/env python3
import argparse
import json

import iterm2
import iterm2.api_pb2
import iterm2.rpc


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--session-id")
    parser.add_argument("--text")
    parser.add_argument("--visible", choices=("true", "false"))
    parser.add_argument("--collapsed", choices=("true", "false"))
    parser.add_argument("--raw-json")
    return parser.parse_args()


ARGS = parse_args()


async def main(connection):
    app = await iterm2.async_get_app(connection)
    if ARGS.session_id:
        session = app.get_session_by_id(ARGS.session_id)
    else:
        window = app.current_window
        if window is None or window.current_tab is None:
            raise RuntimeError("No current terminal window")
        session = window.current_tab.current_session
    if session is None:
        raise RuntimeError("Session not found")

    if ARGS.raw_json is not None:
        response = await iterm2.rpc.async_set_property(
            connection,
            "session_note",
            ARGS.raw_json,
            session_id=session.session_id)
        status = response.set_property_response.status
        print(iterm2.api_pb2.SetPropertyResponse.Status.Name(status))
        return

    update = {}
    if ARGS.text is not None:
        update["text"] = ARGS.text
    if ARGS.visible is not None:
        update["visible"] = ARGS.visible == "true"
    if ARGS.collapsed is not None:
        update["collapsed"] = ARGS.collapsed == "true"
    if update:
        await session.async_set_session_note(**update)

    note = await session.async_get_session_note()
    print(json.dumps({
        "session_id": session.session_id,
        "text": note.text,
        "visible": note.visible,
        "collapsed": note.collapsed,
    }, indent=2))


iterm2.run_until_complete(main)
```

Do not add this file to git.

- [ ] **Step 5: Target the custom suite explicitly**

Run every smoke command with `IT2_SUITE=session-note-api`; otherwise the SDK
defaults to the installed app's `iTerm2` socket.

```bash
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --text $'Task: verify Session Notes API\nNext: test collapse' --visible true --collapsed false
```

Expected: the note appears in the current pane, the JSON reports the exact
text, and the terminal retains keyboard focus. Type a harmless character in
the terminal immediately; it must go to the terminal, not the note editor.

- [ ] **Step 6: Exercise every state transition**

Run and visually verify each command:

```bash
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --text $'Task: verify Session Notes API\nProgress: updated live'
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --collapsed true
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --visible false
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --visible true
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --text ''
```

Pass criteria:

- Text updates live while the note is visible.
- Collapse changes without focusing the note.
- Hide preserves text and collapse state.
- Show restores the same text and collapse state without focus theft.
- Empty text hides and deletes the note, and GET returns the canonical empty
  object.

- [ ] **Step 7: Verify user edits are visible through the API**

Create or show a note, click into it manually, edit its text, then run the
smoke script with no patch arguments:

```bash
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py
```

Expected: GET returns the manually edited text.

- [ ] **Step 8: Verify background-pane targeting**

Run the read-only smoke script once in each pane to obtain both session IDs.
Keep pane A active, then update pane B by ID:

```bash
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --session-id SESSION_ID_FROM_PANE_B --text 'Background pane hand-off' --visible true
```

Replace `SESSION_ID_FROM_PANE_B` with the actual ID printed by the script.
Expected: pane B's note changes while pane A remains active and accepts typing.

- [ ] **Step 9: Verify persistence**

Set a visible, collapsed note. Save the window arrangement or use the same
restoration path already supported by Session Notes, quit only the custom app,
then start it again with `make run`.

Expected: the note's text and collapsed state restore exactly as a manually
created Session Note would. Re-run GET to verify the API snapshot matches the
restored UI.

- [ ] **Step 10: Verify validation failures manually**

First seed a note whose unchanged value is easy to recognize, then send each
invalid payload through the generic RPC:

```bash
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --text 'validation baseline' --visible true --collapsed false
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --raw-json '{}'
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --raw-json '{"unknown":true}'
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --raw-json '{"visible":1}'
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py --raw-json '{"text":"","visible":true}'
IT2_SUITE=session-note-api /tmp/iterm2-session-note-sdk/bin/python /tmp/iterm2-session-note-smoke.py
```

Expected: each raw update prints `INVALID_VALUE`; the final GET still reports
`validation baseline`, visible and expanded. The synthetic-session test covers
the no-live-view `IMPOSSIBLE` case without depending on UI teardown timing.

- [ ] **Step 11: Stop the custom app and confirm no smoke artifact is staged**

Quit the custom iTerm2 normally, then:

```bash
git status --short
git diff --check
```

Expected: only intended source, test, and documentation changes are present;
`/tmp/iterm2-session-note-smoke.py` is outside the repository.

## Task 8: Run complete verification and review the PR-sized diff

**Files:** all files changed above.

- [ ] **Step 1: Run the full ModernTests suite**

```bash
tools/run_tests.expect -parallel
```

Expected: all ModernTests pass. If parallel output reports a failure, rerun the
named test without `-parallel` to capture the real assertion before changing
code.

- [ ] **Step 2: Re-run the SDK suite and lint**

```bash
cd api/library/python/iterm2
python3 -m pytest tests/ -v
make pylint
cd ../../../..
```

Expected: all SDK tests pass and pylint reports no new errors.

- [ ] **Step 3: Rebuild from the final tree**

```bash
tools/build.sh Development
```

Expected: successful Development build with no warnings.

- [ ] **Step 4: Inspect scope and generated-file boundaries**

```bash
git status --short
git diff --check origin/master...HEAD
git diff --stat origin/master...HEAD
git diff --name-only origin/master...HEAD
git log --oneline origin/master..HEAD
```

Expected changed paths are limited to:

```text
ModernTests/SessionNoteAPITests.swift
api/library/python/iterm2/docs/session.rst
api/library/python/iterm2/iterm2/__init__.py
api/library/python/iterm2/iterm2/api_pb2.py (only if regenerated comments change it)
api/library/python/iterm2/iterm2/api_pb2.pyi (only if regenerated comments change it)
api/library/python/iterm2/iterm2/session.py
api/library/python/iterm2/tests/test_session.py
docs/notes-3.7.txt
iTerm2.xcodeproj/project.pbxproj
proto/api.proto
sources/API/iTermAPIHelper.m
sources/PTYSession/PTYSession.swift
sources/SessionNotes/SessionNoteModel.swift
sources/proto/Api.pbobjc.h (only if regenerated comments change it)
sources/proto/Api.pbobjc.m (only if regenerated comments change it)
sources/TerminalView/SessionView.h
sources/TerminalView/SessionView.m
```

The protobuf diff must be limited to property documentation; generated changes
must be comment-only and traceable to `tools/build_proto.sh`. There must be no
protobuf schema change, claude-monitor code, packaged `.its` file, local build
product, or AI-authored plan in the upstream branch.

- [ ] **Step 5: Review the complete behavior against the contract**

Confirm every Product contract rule at the top of this document has either an
automated test or a recorded manual result. Pay particular attention to:

- all-or-nothing validation;
- hidden-note preservation;
- clear/delete semantics;
- exact background-pane targeting;
- no keyboard-focus change;
- no context sent to Claude or any remote service.

## Task 9: Prepare and submit the upstream pull request

**Files:** no new product files unless review finds a defect.

- [ ] **Step 1: Create or connect the GitHub fork**

From the iTerm2 worktree:

```bash
gh repo fork gnachman/iTerm2 --remote --remote-name fork
git remote -v
```

Expected: `origin` remains the upstream repository and `fork` points to the
authenticated user's fork.

- [ ] **Step 2: Run the required pre-push review**

Load and follow the `pre-push-check` skill. It owns the local Fresh Eyes review
and its macOS-safe background launch. Resolve actionable findings, rerun the
relevant focused tests, and repeat the final verification if code changes.

- [ ] **Step 3: Push the contributor branch**

```bash
git push -u fork ct/session-note-api
```

- [ ] **Step 4: Create the PR with the repository's required concise format**

Load and follow the `concise-pr` skill before writing any PR text. Create one PR
against `gnachman/iTerm2:master`. Use the authenticated fork owner dynamically:

```bash
gh pr create \
  --repo gnachman/iTerm2 \
  --base master \
  --head "$(gh api user --jq .login):ct/session-note-api"
```

The PR description should state:

- Session Notes can be read and patched through the Python API.
- API-driven show/update operations preserve terminal focus.
- The implementation reuses the generic session-property RPC and does not
  change the protobuf schema.
- Automated ModernTests, SDK tests/lint, Development build, and isolated-suite
  manual checks performed.

Describe the capability generically for all automation clients. claude-monitor
is a concrete motivation, not an iTerm2 dependency.

- [ ] **Step 5: Run the post-PR check**

Load and follow `pre-push-check` again after the PR exists so its automated
Fresh Eyes watch is attached. Record the PR URL and current CI status in the
implementation hand-off.

## Final hand-off checklist

- [ ] Upstream repo cloned from `gnachman/iTerm2` with submodules.
- [ ] Work performed on `ct/session-note-api` in
      `.Codex/worktrees/ct/session-note-api`.
- [ ] `session_note` GET returns the canonical three-field snapshot.
- [ ] `session_note` SET is a strict, atomic patch operation.
- [ ] API updates never focus the note or submit terminal input.
- [ ] Python SDK has typed get/set methods and a `SessionNote` value.
- [ ] Focused and full app tests pass.
- [ ] Full Python SDK tests and lint pass.
- [ ] Final Development build passes without warnings.
- [ ] Custom `make run` build passes foreground, background-pane, collapse,
      hide/show, manual-edit, clear, validation, and persistence checks.
- [ ] No protobuf schema change and no claude-monitor dependency; any generated
      diff is comment-only.
- [ ] Release note is user-facing and every line is at most 50 columns.
- [ ] Pre-push and post-PR checks complete.
- [ ] One reviewable upstream PR opened against `master`.
