// Compile separate expression/need axes into addressable display combinations.
// The combination count is not the number of independently trained intent classes.
export function compileSocialIntents(source, expressions) {
  const groups = [];
  const needs = [];
  const ids = new Set();
  const names = new Set();
  if (!source.version || !Array.isArray(source.groups)) throw new Error('Invalid social intent source');
  for (const group of source.groups) {
    if (!group.id || !group.label || !group.modelLabel || groups.some(item => item.id === group.id)
      || !Array.isArray(group.leaves) || group.leaves.length < 2 || group.leaves.length > 12) {
      throw new Error(`Invalid social intent group: ${group.id}`);
    }
    groups.push({ id: group.id, label: group.label, modelLabel: group.modelLabel });
    const modelLabels = new Set();
    for (const leaf of group.leaves) {
      const id = `${group.id}_${leaf.id}`;
      if (!/^[a-z][a-z0-9_]*$/.test(id) || ids.has(id) || !leaf.label || Array.from(leaf.label).length > 6
        || names.has(leaf.label) || !leaf.modelLabel || !leaf.definition || modelLabels.has(leaf.modelLabel)) {
        throw new Error(`Invalid social intent: ${id}`);
      }
      ids.add(id);
      names.add(leaf.label);
      modelLabels.add(leaf.modelLabel);
      needs.push({ id, group: group.id, label: leaf.label, modelLabel: leaf.modelLabel, definition: leaf.definition });
    }
  }
  const combinations = expressions.flatMap(expression => needs.map(need => ({
    id: `playful:${expression.id}:${need.id}`,
    expressionId: expression.id,
    needId: need.id,
    label: `${expression.label}·${need.label}`,
    shortLabel: need.label,
  })));
  return {
    socialIntentVersion: source.version,
    socialIntentGroups: groups,
    socialIntents: needs,
    playfulIntents: combinations,
    vocabularyCounts: {
      expressions: expressions.length,
      socialNeeds: needs.length,
      expressionNeedCombinations: combinations.length,
    },
  };
}
