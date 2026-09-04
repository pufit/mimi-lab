// The sentences of a moment, in the order they are heard (clip navigation).
//
// A card's clip covers the *moment*: the evidence lines the meaning leans on,
// the target sentence, and the cue the sentence spills into. For ←/→/↓ (Migaku's
// subtitle keys: previous / next / replay the current one) that clip is a list
// of sentences, each with its own span on the episode's clock:
//
//   * every `evidence` line is one sentence;
//   * the target cue plus its `continuation` cues is ONE sentence — the
//     continuation is the same utterance running into the next cue, and the
//     card front never shows it on its own.
//
// Times stay in episode ms here; mapping onto the media's own clock (a cut
// clip starts at `clip.start_ms`, the streamed episode at 0) is the player's
// business — see `use-sentence-playback.ts`.

import { lineRole } from "@/lib/srs-extend";
import type { SrsCard } from "@/lib/srs-types";

export interface Sentence {
  /** Stable React key. */
  key: string;
  line_id: number | null;
  /** The sentence being recalled, or a neighbour it leans on. */
  role: "target" | "evidence";
  text: string;
  /** Episode ms. */
  start_ms: number;
  /** Episode ms — the target's end is the end of its last continuation cue. */
  end_ms: number;
}

/** The highlight moves onto a sentence a touch before its audio starts. */
export const ACTIVE_LEAD_MS = 200;

export type SentenceCard = Pick<
  SrsCard,
  "line_id" | "start_ms" | "end_ms" | "text" | "extend"
>;

/** Chronological sentences of the moment; the target is always present. */
export function momentSentences(card: SentenceCard): Sentence[] {
  let start = card.start_ms;
  let end = Math.max(card.end_ms, card.start_ms);
  const seen = new Set<number>();
  if (card.line_id != null) seen.add(card.line_id);

  const evidence: Sentence[] = [];
  for (const line of card.extend) {
    if (line.line_id != null) {
      if (seen.has(line.line_id)) continue;
      seen.add(line.line_id);
    }
    if (lineRole(line) === "continuation") {
      // Same utterance, next cue: it widens the target sentence.
      if (line.start_ms < start) start = line.start_ms;
      if (line.end_ms > end) end = line.end_ms;
      continue;
    }
    evidence.push({
      key: line.line_id != null ? `line-${line.line_id}` : `ev-${line.idx ?? ""}-${line.start_ms}`,
      line_id: line.line_id,
      role: "evidence",
      text: line.text,
      start_ms: line.start_ms,
      end_ms: Math.max(line.end_ms, line.start_ms),
    });
  }

  const target: Sentence = {
    key: card.line_id != null ? `line-${card.line_id}` : "target",
    line_id: card.line_id,
    role: "target",
    text: card.text,
    start_ms: start,
    end_ms: end,
  };

  return [...evidence, target].sort(
    (a, b) => a.start_ms - b.start_ms || (a.role === "target" ? -1 : 1),
  );
}

/**
 * Which sentence is playing at `ms` (episode clock): the last one that has
 * started, `ACTIVE_LEAD_MS` early. Before the first sentence it is the first
 * one; after the last it stays the last (the highlight does not go dark in a
 * pause between two lines). `-1` only when there are no sentences at all.
 */
export function activeSentenceIndex(sentences: Sentence[], ms: number): number {
  if (sentences.length === 0) return -1;
  let idx = 0;
  for (let i = 0; i < sentences.length; i++) {
    if (sentences[i].start_ms - ACTIVE_LEAD_MS <= ms) idx = i;
    else break;
  }
  return idx;
}
