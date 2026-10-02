"use strict";
(function () {
  const bank = {
    happy: { category: "愉快", variants: ["(＾▽＾)", "(≧▽≦)", "(⌒‿⌒)"] },
    affectionate: { category: "亲近", variants: ["(｡♥‿♥｡)", "(♡˙︶˙♡)", "( ˘ ³˘)♥"] },
    neutral: { category: "平静", variants: ["( ˘͈ ᵕ ˘͈ )", "(￣ー￣)", "( ´ ▽ ` )"] },
    amused: { category: "有趣", variants: ["(｡•̀ᴗ-)✧", "(≧∇≦)ﾉ", "( ´艸｀)"] },
    sad: { category: "难过", variants: ["(´；ω；`)", "(╥﹏╥)", "(｡•́︿•̀｡)"] },
    anxious: { category: "不安", variants: ["(；´Д｀)", "(｡•́︿•̀｡)", "(；￣Д￣)"] },
    angry: { category: "生气", variants: ["(｀ε´)", "(╬ಠ益ಠ)", "(＃｀д´)ﾉ"] },
    excited: { category: "兴奋", variants: ["(๑•̀ㅂ•́)و✧", "(ﾉ◕ヮ◕)ﾉ", "(≧∇≦)ﾉ"] },
    surprised: { category: "惊讶", variants: ["(⊙_⊙)", "(ﾟДﾟ)", "(°o°)"] },
    shy: { category: "害羞", variants: ["(*/ω＼*)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)", "(〃▽〃)"] },
    grateful: { category: "感谢", variants: ["(人´∀｀)", "( ˘ ³˘)♥", "(｡•̀ᴗ-)✧"] },
    content: { category: "满足", variants: ["(￣▽￣)", "( ˘͈ ᵕ ˘͈ )", "(๑´ڡ`๑)"] },
    relieved: { category: "释然", variants: ["( ´ ▽ ` )", "(￣▽￣)ノ", "( ˘ω˘ )"] },
    hopeful: { category: "期待", variants: ["(☆▽☆)", "(๑•̀ㅂ•́)و✧", "( ˙꒳˙ )✧"] },
    curious: { category: "好奇", variants: ["(・_・?)", "(◎_◎;)", "(｡･ω･｡)?"] },
    proud: { category: "自豪", variants: ["(￣^￣)ゞ", "(๑˃ᴗ˂)ﻭ", "( •̀ᄇ•́)ﻭ✧"] },
    tender: { category: "温柔", variants: ["(づ｡◕‿‿◕｡)づ", "( ˘ ³˘)♥", "(｡･ω･｡)ﾉ♡"] },
    lonely: { category: "孤单", variants: ["(｡•́︿•̀｡)", "( ´•̥̥̥ω•̥̥̥` )", "(つ﹏⊂)"] },
    disappointed: { category: "失望", variants: ["(︶︹︺)", "(｡•́︿•̀｡)", "(╯︵╰,)"] },
    hurt: { category: "受伤", variants: ["(╥﹏╥)", "(｡•́︿•̀｡)", "(つ﹏⊂)"] },
    guilty: { category: "愧疚", variants: ["(；´д｀)ゞ", "(._.)", "(｡•́︿•̀｡)"] },
    ashamed: { category: "羞愧", variants: ["(〃￣ω￣〃ゞ", "(*/_＼)", "(⁄ ⁄•⁄ω⁄•⁄ ⁄)"] },
    fearful: { category: "害怕", variants: ["(ﾟДﾟ;)", "(；ﾟДﾟ)", "(⊙﹏⊙)"] },
    worried: { category: "担忧", variants: ["(；￣Д￣)", "(｡•́︿•̀｡)", "(；´Д｀)"] },
    stressed: { category: "紧绷", variants: ["(；´Д｀)", "(＞﹏＜)", "(；￣Д￣)"] },
    overwhelmed: { category: "不堪重负", variants: ["(＠_＠)", "(×_×)", "(；´Д｀)"] },
    frustrated: { category: "挫败", variants: ["(ノ_<。)", "(╯︵╰,)", "(＞﹏＜)"] },
    irritated: { category: "烦躁", variants: ["(눈_눈)", "(¬_¬)", "(｀ε´)"] },
    resentful: { category: "怨怼", variants: ["(눈_눈)", "(¬_¬)", "(｀Д´)"] },
    jealous: { category: "吃醋", variants: ["(¬_¬)", "(￣へ￣)", "(눈_눈)"] },
    suspicious: { category: "怀疑", variants: ["(¬_¬)", "(￢_￢)", "(눈_눈)"] },
    confused: { category: "困惑", variants: ["(・_・?)", "(⊙_☉)", "(・・;)ゞ"] },
    uncertain: { category: "犹豫", variants: ["(・_・;)", "(￣～￣;)", "(・・;)ゞ"] },
    bored: { category: "无聊", variants: ["(￣ー￣)", "(－_－)", "( ´△｀)"] },
    detached: { category: "淡漠", variants: ["(￣ー￣)", "(－_－)", "(￢_￢)"] },
    calm: { category: "安定", variants: ["( ˘͈ ᵕ ˘͈ )", "( ˘ω˘ )", "( ´ ▽ ` )"] },
    focused: { category: "专注", variants: ["( •̀_•́ )", "(｀・ω・´)", "(ง •̀_•́)ง"] },
    determined: { category: "坚定", variants: ["(ง •̀_•́)ง", "(๑•̀ㅂ•́)و✧", "( •̀ᄇ•́)ﻭ✧"] },
    embarrassed: { category: "尴尬", variants: ["(￣▽￣;)", "(・・;)ゞ", "(〃￣ω￣〃ゞ"] },
    tired: { category: "疲惫", variants: ["(￣o￣) . z Z", "(－_－) zzZ", "( ´△｀)"] }
  };
  const legacy = {
    happy: ["开心", "快乐"], affectionate: ["亲近", "喜爱"], neutral: ["平静", "中性"],
    amused: ["有趣", "搞怪"], sad: ["难过", "悲伤"], anxious: ["不安", "焦虑"],
    angry: ["生气", "愤怒"], excited: ["兴奋"], surprised: ["惊讶"], shy: ["害羞"],
    grateful: ["感谢"], tired: ["疲惫"]
  };
  const categoryGroups = {
    "愉快与期待": ["happy", "amused", "excited", "content", "relieved", "hopeful", "proud"],
    "亲近与感激": ["affectionate", "grateful", "tender", "shy"],
    "平和与专注": ["neutral", "calm", "focused", "determined"],
    "好奇与意外": ["curious", "surprised", "confused", "uncertain", "embarrassed"],
    "低落与失落": ["sad", "lonely", "disappointed", "hurt", "guilty", "ashamed"],
    "担忧与压力": ["anxious", "fearful", "worried", "stressed", "overwhelmed"],
    "冲突与不悦": ["angry", "frustrated", "irritated", "resentful", "jealous", "suspicious"],
    "疲惫与淡漠": ["bored", "tired", "detached"]
  };
  const categoryById = new Map(Object.entries(categoryGroups).flatMap(([category, ids]) => ids.map(id => [id, category])));
  const aliases = new Map();
  const normalize = value => typeof value === "string" ? value.trim().toLowerCase().replace(/[\s-]+/g, "_") : "";
  for (const [id, labels] of Object.entries(legacy)) for (const label of [id, ...labels]) aliases.set(normalize(label), id);
  let catalogEmotions = null;
  function installCatalog(catalog) {
    if (!Array.isArray(catalog?.emotions) || !catalog.version) return { missing: ["catalog unavailable"], total: 0 };
    const missing = catalog.emotions.filter(emotion => !bank[emotion.id] || bank[emotion.id].variants.length < 2).map(emotion => emotion.id);
    if (missing.length) return { missing, total: catalog.emotions.length };
    catalogEmotions = catalog.emotions;
    for (const emotion of catalogEmotions) {
      for (const value of [emotion.id, emotion.modelLabel, emotion.label, ...(emotion.aliases || [])]) {
        if (normalize(value)) aliases.set(normalize(value), emotion.id);
      }
    }
    return { missing: [], total: catalogEmotions.length };
  }
  function stableIndex(key, length) {
    let hash = 2166136261;
    for (const character of String(key)) {
      hash ^= character.charCodeAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0) % length;
  }
  function resolve(value) {
    const candidates = typeof value === "object" && value ? [value.id, value.rawLabel, value.modelLabel, value.label] : [value];
    for (const candidate of candidates) {
      const id = aliases.get(normalize(candidate));
      if (id && bank[id]) return id;
    }
    return null;
  }
  function pick(value, messageId) {
    const id = resolve(value);
    return id ? bank[id].variants[stableIndex(messageId, bank[id].variants.length)] : "";
  }
  function panelItems() {
    const emotions = catalogEmotions ? [...catalogEmotions] : [];
    const ids = new Set(emotions.map(emotion => emotion.id));
    for (const [id, labels] of Object.entries(legacy)) if (!ids.has(id)) emotions.push({ id, label: labels[0] });
    const groups = new Map(Object.keys(categoryGroups).map(category => [category, []]));
    for (const emotion of emotions) {
      const entry = bank[emotion.id];
      if (!entry) continue;
      const category = categoryById.get(emotion.id) || entry.category;
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push({ label: emotion.label, variants: entry.variants });
    }
    return [...groups].filter(([, items]) => items.length).map(([category, items]) => ({ category, items }));
  }
  window.Kaomoji = Object.freeze({ installCatalog, pick, panelItems, resolve });
})();
