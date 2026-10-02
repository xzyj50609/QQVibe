import type { Answer, Question } from "./types";

export const MBTI_QUESTION_VERSION = "mbti-chat-evidence-v3";
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
  EI: ["no stated energy preference", "outward interaction restores energy", "inward reflection restores energy"],
  SN: ["no stated information preference", "concrete facts and experience", "patterns and possibilities"],
  TF: ["no stated decision preference", "logical principles in decisions", "personal values and people's impact"],
  JP: ["no stated external world preference", "structured closure and schedules", "flexible exploration of options"],
} as const;

export const PERSONALITY_QUESTIONS: Record<string, Question> = {
  mbti_scope: {
    type: "choice",
    instructions: "Does the TARGET sender explicitly describe their own enduring or recurring personal preference across situations? A greeting, refusal, kind act, emotion, ordinary plan or isolated fact is NOT preference evidence.",
    criteria: ["no enduring preference stated", "sender states a recurring personal preference"],
  },
  mbti_EI: {
    type: "choice",
    instructions: "Classify only the sender's stated recurring preference for restoring energy. Routine chat or talking a lot is not evidence. If no self-described stable preference, choose no stated preference.",
    criteria: [...OPTIONS.EI],
  },
  mbti_SN: {
    type: "choice",
    instructions: "Classify only the sender's stated recurring preference for taking in information. Mentioning a fact or idea once is not a stable preference. If not self-described, choose no stated preference.",
    criteria: [...OPTIONS.SN],
  },
  mbti_TF: {
    type: "choice",
    instructions: "Classify only the sender's stated recurring basis for making decisions. Emotion alone is not evidence. If there is no self-described stable decision preference, choose no stated preference.",
    criteria: [...OPTIONS.TF],
  },
  mbti_JP: {
    type: "choice",
    instructions: "Classify only the sender's stated recurring preference for organizing the external world. One appointment is not evidence. If no self-described stable preference, choose no stated preference.",
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
  const scope = answers.mbti_scope;
  if (!scope || scope.type !== "choice" ||
    (scope.probabilities["sender states a recurring personal preference"] ?? 0) <=
    (scope.probabilities["no enduring preference stated"] ?? 0)) return null;
  const EI = distribution(answers.mbti_EI, "E", "I", OPTIONS.EI);
  const SN = distribution(answers.mbti_SN, "S", "N", OPTIONS.SN);
  const TF = distribution(answers.mbti_TF, "T", "F", OPTIONS.TF);
  const JP = distribution(answers.mbti_JP, "J", "P", OPTIONS.JP);
  if (!EI || !SN || !TF || !JP) return null;
  const anySupported = EI.E > EI.insufficient || EI.I > EI.insufficient ||
    SN.S > SN.insufficient || SN.N > SN.insufficient ||
    TF.T > TF.insufficient || TF.F > TF.insufficient ||
    JP.J > JP.insufficient || JP.P > JP.insufficient;
  return anySupported ? { EI, SN, TF, JP } : null;
}
