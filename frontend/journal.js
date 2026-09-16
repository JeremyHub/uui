// What the app remembers about the session.
//
// A turn only ever sees the screen and the click that caused it, which is enough to
// answer "what should happen next" and not enough to stay consistent. The user gave
// their name six screens ago, picked vegetarian, said they were shopping for a gift --
// none of that is on screen any more, so without a record it is simply gone, and the app
// starts contradicting itself exactly when a session gets long enough to be worth having.
//
// Keeping every turn verbatim does not work either: the prompt grows without bound and
// output tokens are the expensive part, so a prompt that doubles halves the app. So the
// recent past is kept verbatim and the older past is compacted into prose by the model
// itself -- during idle time, where it costs the user nothing.

const COMPACTION_PROMPT = `You keep the running memory of a session with a generated app.

Given the story so far and a list of things the user has since done, write the new story
so far.

Keep anything later screens would need to stay consistent: what the user is trying to do,
choices they have made, values they entered, things they were shown and might refer back
to. Drop anything already superseded -- if they changed a filter twice, only the last one
is true.

Write 2-5 sentences of plain prose, in the third person ("the user ..."). No lists, no
headings, no commentary about this task.`;

export class Journal {
  /**
   * @param verbatim how many recent turns to send word for word. Enough to carry a
   *   short back-and-forth; past that, prose is both smaller and more useful.
   */
  constructor({ verbatim = 6, compactAfter = 10 } = {}) {
    this.verbatim = verbatim;
    this.compactAfter = compactAfter;
    this.entries = [];       // { label, plan, inputs } oldest first
    this.summary = "";       // the compacted older past
    this.compacting = false;
  }

  add({ label, plan, inputs }) {
    // Only what the user actually supplied. Empty boxes and unticked checkboxes are the
    // bulk of a form's state and say nothing about intent.
    const supplied = Object.entries(inputs ?? {})
      .filter(([, v]) => v !== "" && v !== false && v !== null && v !== undefined)
      .map(([k, v]) => `${k}=${JSON.stringify(v)}`)
      .join(", ");
    this.entries.push({ label, plan: plan || "", inputs: supplied });
  }

  line(entry) {
    const parts = [entry.label];
    if (entry.inputs) parts.push(`(entered ${entry.inputs})`);
    if (entry.plan) parts.push(`-> ${entry.plan}`);
    return parts.join(" ");
  }

  /** The memory as the model sees it: prose for the old, verbatim for the recent. */
  toPrompt() {
    const sections = [];
    if (this.summary) sections.push(`STORY SO FAR:\n${this.summary}`);
    const recent = this.entries.slice(-this.verbatim);
    if (recent.length) {
      sections.push("RECENTLY:\n" + recent.map((e) => `- ${this.line(e)}`).join("\n"));
    }
    return sections.join("\n\n");
  }

  needsCompaction() {
    return !this.compacting && this.entries.length > this.compactAfter;
  }

  /**
   * Fold everything older than the verbatim window into the summary.
   *
   * Runs in idle time after a turn, alongside the guessing, so the user never waits for
   * it. On failure the journal is left exactly as it was -- losing the older past
   * quietly would be worse than a prompt that stays a bit too long.
   */
  async compact(transport, signal) {
    if (!this.needsCompaction()) return false;
    const older = this.entries.slice(0, -this.verbatim);
    if (!older.length) return false;

    this.compacting = true;
    try {
      const user = [
        `STORY SO FAR:\n${this.summary || "(nothing yet -- this is the beginning)"}`,
        "SINCE THEN:\n" + older.map((e) => `- ${this.line(e)}`).join("\n"),
      ].join("\n\n");

      let text = "";
      for await (const piece of transport.chat({
        system: COMPACTION_PROMPT, user, maxTokens: 220, temperature: 0.2, signal,
      })) {
        text += piece;
      }
      text = text.trim();
      if (!text) return false;
      this.summary = text;
      this.entries = this.entries.slice(-this.verbatim);
      return true;
    } catch {
      return false;
    } finally {
      this.compacting = false;
    }
  }
}
