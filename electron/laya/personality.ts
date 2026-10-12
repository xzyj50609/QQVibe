import type { Answer, Question } from "./types";

export const MBTI_QUESTION_VERSION = "mbti-chat-evidence-v4";
export const MBTI_SOURCES = [
  "https://www.myersbriggs.org/my-mbti-personality-type/the-mbti-preferences/",
  "https://www.themyersbriggs.com/en-US/Products-and-Services/Myers-Briggs",
] as const;

export interface PersonalityEvidence {
  EI: { E: number; I: number; insufficient: number };
  SN: { S: number; N: number; insufficient: number };
  TF: { T: number; F: number; insufficient: number };
  JP: { J: number; P: number; insufficient: number };
}
const OPTIONS = {
  EI: ["insufficient energy preference evidence", "outward interaction restores energy", "inward reflection restores energy"],
  SN: ["insufficient information preference evidence", "concrete facts and experience", "patterns and possibilities"],
  TF: ["insufficient decision preference evidence", "logical principles in decisions", "personal values and people's impact"],
  JP: ["insufficient organization preference evidence", "structured closure and schedules", "flexible exploration of options"],
} as const;

const evidenceRule = "Use an explicit self-description OR a recurring pattern of the TARGET sender's own freely chosen preferences across distinct situations in the supplied context. The sender need not explicitly call it a long-term habit. A single concrete preference can support a tentative axis score; one routine act cannot. Judge this axis independently. Use only the TARGET sender's own words and choices; quotes, roleplay, other people's preferences and the partner's suggestions alone are not evidence. Repeated copies of the same example do not add independent support. Weigh counterexamples and uncertainty; do not force a pole or fill missing context. If unsupported, choose the insufficient evidence option.";

export const PERSONALITY_QUESTIONS: Record<string, Question> = {
  mbti_EI: {
    type: "choice",
    instructions: "What restores the TARGET sender's energy: outward interaction or inward reflection? Talking a lot, replying quickly, sociability, being tired today or declining one invitation do not establish this preference. " + evidenceRule,
    criteria: [...OPTIONS.EI],
  },
  mbti_SN: {
    type: "choice",
    instructions: "How does the TARGET sender prefer to take in information: concrete facts and experience, or patterns and possibilities? Consider how they repeatedly seek or compare information, not merely whether the topic mentions a fact or an idea. " + evidenceRule,
    criteria: [...OPTIONS.SN],
  },
  mbti_TF: {
    type: "choice",
    instructions: "What basis does the TARGET sender prefer when making choices or tradeoffs: logical principles, or personal values and people's impact? Weigh their reasons, not the topic. Politeness, expressing emotion or caring for someone alone is not evidence. " + evidenceRule,
    criteria: [...OPTIONS.TF],
  },
  mbti_JP: {
    type: "choice",
    instructions: "When free to choose, does the TARGET sender prefer structured closure and schedules, or flexible exploration of options? Consider repeated voluntary choices. A required deadline, doing assigned work or making one appointment alone is not evidence. " + evidenceRule,
    criteria: [...OPTIONS.JP],
  },
};

function distribution<Left extends string, Right extends string>(
  answer: Answer | undefined,
  left: Left,
  right: Right,
  options: readonly [string, string, string],
): Record<Left | Right | "insufficient", number> | null {
  if (!answer || answer.type !== "choice") return null;
  const probabilities = answer.probabilities;
  if (!options.every((key) =>
    Number.isFinite(probabilities[key]) && probabilities[key] >= 0 && probabilities[key] <= 1
  )) return null;
  return { [left]: probabilities[options[1]], [right]: probabilities[options[2]], insufficient: probabilities[options[0]] } as Record<Left | Right | "insufficient", number>;
}

export function personalityEvidenceFromAnswers(answers: Record<string, Answer>): PersonalityEvidence | null {
  // Keep the wire shape, but never let an absent/invalid axis erase another.
  // Unknown axes contain no pole mass; they are not invented 50/50 scores.
  const EI = distribution(answers.mbti_EI, "E", "I", OPTIONS.EI) ?? { E: 0, I: 0, insufficient: 1 };
  const SN = distribution(answers.mbti_SN, "S", "N", OPTIONS.SN) ?? { S: 0, N: 0, insufficient: 1 };
  const TF = distribution(answers.mbti_TF, "T", "F", OPTIONS.TF) ?? { T: 0, F: 0, insufficient: 1 };
  const JP = distribution(answers.mbti_JP, "J", "P", OPTIONS.JP) ?? { J: 0, P: 0, insufficient: 1 };
  const anySupported = EI.E > EI.insufficient || EI.I > EI.insufficient ||
    SN.S > SN.insufficient || SN.N > SN.insufficient ||
    TF.T > TF.insufficient || TF.F > TF.insufficient ||
    JP.J > JP.insufficient || JP.P > JP.insufficient;
  return anySupported ? { EI, SN, TF, JP } : null;
}
