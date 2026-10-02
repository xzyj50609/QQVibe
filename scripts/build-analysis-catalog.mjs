import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { compileSocialIntents } from './social-intent-catalog.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = JSON.parse(readFileSync(path.join(root, 'scripts/analysis-catalog-source.json'), 'utf8'));
const socialSource = JSON.parse(readFileSync(path.join(root, 'scripts/social-intent-source.json'), 'utf8'));
const displaySource = JSON.parse(readFileSync(path.join(root, 'scripts/intent-display-source.json'), 'utf8'));
if (!displaySource.version || !displaySource.groups
  || Object.keys(displaySource.groups).length !== source.intentGroups.length) {
  throw new Error('Intent display groups must cover the routing catalog');
}
const legacyIntentLabels = displaySource.legacyAliases ?? {};
if (!legacyIntentLabels || typeof legacyIntentLabels !== 'object' || Array.isArray(legacyIntentLabels)
  || Object.entries(legacyIntentLabels).some(([alias, label]) =>
    !alias.trim() || typeof label !== 'string' || !label.trim())) {
  throw new Error('Invalid legacy intent display aliases');
}
const intents = [];
const ids = new Set();
for (const group of source.intentGroups) {
  const display = displaySource.groups[group.id];
  const slugs = new Set(group.leaves.map((entry) => entry.split('|')[0]));
  if (!display || typeof display.default !== 'string' || !display.default.trim()
    || [...Object.entries(display.overrides ?? {})].some(([slug, label]) =>
      !slugs.has(slug) || typeof label !== 'string' || !label.trim())) {
    throw new Error(`Invalid intent display group: ${group.id}`);
  }
  for (const entry of group.leaves) {
    const [slug, label, modelLabel, definition] = entry.split('|');
    const id = `${group.id}_${slug}`;
    if (ids.has(id) || !slug || !label || !modelLabel || !definition) throw new Error(`Invalid intent: ${id}`);
    ids.add(id);
    intents.push({ id, group: group.id, label, displayLabel: display.overrides?.[slug] ?? display.default,
      modelLabel, definition });
  }
}
if (intents.length < 500) throw new Error(`Only ${intents.length} intent leaves`);
const emotionIds = new Set(source.emotions.map((emotion) => emotion.id));
const expressionFamilyIds = new Set();
const expressionGroupFamilies = new Map();
for (const family of source.expressionFamilies) {
  if (!family.id || !family.label || !family.modelLabel || expressionFamilyIds.has(family.id)
    || !Array.isArray(family.groups) || family.groups.length < 1 || family.groups.length > 4) {
    throw new Error(`Invalid expression family: ${family.id}`);
  }
  expressionFamilyIds.add(family.id);
  for (const groupId of family.groups) {
    if (expressionGroupFamilies.has(groupId)) throw new Error(`Expression group assigned twice: ${groupId}`);
    expressionGroupFamilies.set(groupId, family.id);
  }
}
const expressionGroups = [];
const expressions = [];
const expressionIds = new Set();
const facesByEmotion = new Map();
for (const bucket of source.expressionFaceBuckets) {
  if (!Array.isArray(bucket.kaomoji) || bucket.kaomoji.length < 1
    || bucket.kaomoji.some((face) => typeof face !== 'string' || !face.trim())) {
    throw new Error('Invalid expression face bucket');
  }
  for (const emotion of bucket.emotions) {
    if (!emotionIds.has(emotion) || facesByEmotion.has(emotion)) throw new Error(`Invalid expression face emotion: ${emotion}`);
    facesByEmotion.set(emotion, bucket.kaomoji);
  }
}
if (facesByEmotion.size !== emotionIds.size) throw new Error('Expression faces do not cover every emotion');
for (const group of source.expressionGroups) {
  const family = expressionGroupFamilies.get(group.id);
  if (!family || !group.id || !group.label || !group.modelLabel
    || !Array.isArray(group.leaves) || group.leaves.length < 2 || group.leaves.length > 8
    || expressionGroups.some((item) => item.id === group.id)) {
    throw new Error(`Invalid expression group: ${group.id}`);
  }
  expressionGroups.push({ id: group.id, family, label: group.label, modelLabel: group.modelLabel });
  const modelLabels = new Set();
  for (const entry of group.leaves) {
    const fields = entry.split('|');
    const [slug, label, modelLabel, definition, emotion] = fields;
    const id = `${group.id}_${slug}`;
    if (fields.length !== 5 || !/^[a-z][a-z0-9_]*$/.test(id) || expressionIds.has(id)
      || ids.has(id) || !label || !modelLabel || !definition || !emotionIds.has(emotion)
      || modelLabels.has(modelLabel)) {
      throw new Error(`Invalid expression: ${id}`);
    }
    expressionIds.add(id);
    modelLabels.add(modelLabel);
    expressions.push({ id, group: group.id, label, modelLabel, definition, emotion, kaomoji: [...facesByEmotion.get(emotion)] });
  }
}
if (expressionGroups.length !== expressionGroupFamilies.size) throw new Error('Missing expression group');
if (expressions.length < 80 || expressions.length > 120) throw new Error(`Expected 80-120 expressions, got ${expressions.length}`);
const catalog = {
  version: `${source.version}+${socialSource.version}`,
  intentDisplayVersion: displaySource.version,
  labelRevision: displaySource.version,
  legacyIntentLabels,
  intentGroups: source.intentGroups.map(({ id, label, modelLabel }) => ({ id, label, modelLabel })),
  emotions: source.emotions,
  intents,
  expressionFamilies: source.expressionFamilies,
  expressionGroups,
  expressions,
  ...compileSocialIntents(socialSource, expressions),
};
mkdirSync(path.join(root, 'chatui/data'), { recursive: true });
writeFileSync(path.join(root, 'chatui/data/analysis-catalog.json'), `${JSON.stringify(catalog, null, 2)}\n`);
console.log(JSON.stringify({ intents: intents.length, emotions: catalog.emotions.length, ...catalog.vocabularyCounts }));
