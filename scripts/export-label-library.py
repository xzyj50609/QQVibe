"""Export the shipped vocabulary only; never read account or conversation data."""
import argparse
import ast
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()
catalog_path = root/'chatui/data/analysis-catalog.json'
catalog = json.loads(catalog_path.read_text(encoding='utf-8-sig'))
faces = {}
for match in re.finditer(r'^\s+([a-z_]+): \{ category: (".*?"), variants: (\[.*\]) \},?$',
                         (root/'chatui/kaomoji.js').read_text(encoding='utf-8'), re.M):
    faces[match[1]] = {'category': json.loads(match[2]), 'variants': json.loads(match[3])}
assert set(faces) == {item['id'] for item in catalog['emotions']}
counts = {name: len(catalog[name]) for name in
          ('emotions', 'intents', 'expressions', 'socialIntents', 'playfulIntents')}
lines = [
    'WechatVibe｜现有标签词库与扩充用资料',
    f'导出时间：{datetime.now():%Y-%m-%d %H:%M}',
    f'词库版本：{catalog["version"]}',
    f'显示简称版本：{catalog["intentDisplayVersion"]}',
    '',
    '这份文件仅导出程序现有词库，不包含任何真实聊天记录、联系人或账号资料。',
    f'基础条目：情绪 {counts["emotions"]}；细分意图 {counts["intents"]}；表达方式 {counts["expressions"]}；趣味/人际需求 {counts["socialIntents"]}。',
    f'另有 {counts["playfulIntents"]} 个表达×需求组合（{counts["expressions"]}×{counts["socialIntents"]}），这是组合词表，不等于同等数量的独立训练类别或人类样本。',
    '附带完整颜文字映射、已有召回用语、关系信号和画像维度说明。',
    '',
    '扩充方向',
    '扩充真实中文聊天中的细腻情绪、表达方式和交往意图，例如撒娇、卖萌、生气、赌气、委屈、吃醋、试探、欲擒故纵、想抱抱、想喊妈妈等。',
    '情绪＝此时的感受；表达方式＝说话的语气和方式；意图＝这句话想实现什么；人际需求＝希望谁对谁做什么。请区分这四层。',
    '显示名称保持简短自然；相近类别必须给区分点，避免堆同义词凑数量。新类别不要覆盖已有ID。',
    '每条新增建议提供：类别、建议分组、唯一ID、中文名称、显示简称、英文模型选项、定义、典型说法、易混淆反例、上下文区分点。',
    '程序使用本地分层分类，性能优先；请保留粗类→细类的层级，不把全部细类塞进一次模型请求。',
    '现有短名称有意合并，例如多个分享类叶子在前端都显示“分享”；不同细类仍由ID、模型选项和定义区分。',
    '',
]

def heading(text):
    lines.extend(['', '=' * 64, text, '=' * 64])

heading(f'一、情绪｜{counts["emotions"]}项；附完整{sum(len(face["variants"]) for face in faces.values())}个颜文字位置（不同情绪可复用同一颜文字）')
for item in catalog['emotions']:
    face = faces[item['id']]
    lines.extend([f'{item["id"]} | {item["label"]} | 模型选项：{item["modelLabel"]}',
                  f'  颜文字：{json.dumps(face["variants"], ensure_ascii=False)}'])

heading(f'二、细分意图｜{len(catalog["intentGroups"])}组、{counts["intents"]}项')
for group in catalog['intentGroups']:
    lines.extend(['', f'【{group["label"]}】{group["id"]} | 模型组名：{group["modelLabel"]}'])
    for item in catalog['intents']:
        if item['group'] != group['id']:
            continue
        lines.extend([f'{item["id"]} | 细类：{item["label"]} | 显示：{item["displayLabel"]}',
                      f'  模型选项：{item["modelLabel"]}', f'  原有定义：{item["definition"]}'])

heading(f'三、表达方式｜{len(catalog["expressionFamilies"])}大类、{len(catalog["expressionGroups"])}组、{counts["expressions"]}项')
for family in catalog['expressionFamilies']:
    lines.extend(['', f'【大类：{family["label"]}】{family["id"]} | {family["modelLabel"]}'])
    for group in catalog['expressionGroups']:
        if group['family'] != family['id']:
            continue
        lines.append(f'  【分组：{group["label"]}】{group["id"]} | {group["modelLabel"]}')
        for item in catalog['expressions']:
            if item['group'] != group['id']:
                continue
            lines.extend([f'{item["id"]} | {item["label"]}',
                          f'  模型选项：{item["modelLabel"]}', f'  原有定义：{item["definition"]}',
                          f'  关联情绪ID：{item["emotion"]} | 现有表达颜文字：{json.dumps(item["kaomoji"], ensure_ascii=False)}'])

heading(f'四、趣味与人际需求｜{len(catalog["socialIntentGroups"])}组、{counts["socialIntents"]}项')
for group in catalog['socialIntentGroups']:
    lines.extend(['', f'【{group["label"]}】{group["id"]} | {group["modelLabel"]}'])
    for item in catalog['socialIntents']:
        if item['group'] != group['id']:
            continue
        lines.extend([f'{item["id"]} | {item["label"]}',
                      f'  模型选项：{item["modelLabel"]}', f'  原有定义：{item["definition"]}'])

heading('五、已有字面召回用语（只召回候选，不直接决定标签或概率）')
cue_source = (root/'electron/laya/social-cues.ts').read_text(encoding='utf-8')
for match in re.finditer(r"\{ tokens: (\[[^\n]+?\]), needs: (\[[^\n]+?\]) \}", cue_source):
    lines.append('、'.join(ast.literal_eval(match[1])) + ' → ' + ' / '.join(ast.literal_eval(match[2])))

heading('六、独立画像维度与关系信号（不要混成消息情绪标签）')
lines.extend([
    '互动风格：socialEnergy=表达活力；humor=幽默表达；composure=情绪平和；initiative=话题主动；care=关怀支持；affection=亲近表达。',
    '关系模型选项：romantic（浪漫兴趣）、warm（友善亲近）、neutral（普通交流）、distant（疏远）、rejecting（拒绝）。',
    'MBTI聊天画像保留EI/SN/TF/JP四维，包含证据不足选项；它是独立的人格推测功能，不是情绪或消息意图标签库。',
    '预测下一句采用粗粒度回应意图；不直接把预测类别当作已经发生的真实消息。',
])

heading(f'七、表达×人际需求完整组合词表｜{counts["playfulIntents"]}项')
lines.extend(['组合ID规则：playful:{expressionId}:{needId}；两部分ID可从上文查到。',
              '下文保留全部组合中文名称；它们由笛卡尔积生成，扩充时可标注不自然的组合，不要把这些组合冒充人工标注样本。'])
for expression in catalog['expressions']:
    lines.extend(['', f'【{expression["label"]}】表达ID={expression["id"]}'])
    lines.extend(item['label'] for item in catalog['playfulIntents'] if item['expressionId'] == expression['id'])

heading('扩充结果交回格式')
lines.extend([
    '请先列出新增/合并/弃用建议，再按“层级、分组、ID、中文名、简称、英文模型选项、定义、正例、反例、区分规则”输出可读词表。',
    '保留既有ID；合并时明确旧ID到新ID的映射。不要把同一个名字换几个措辞后算成多条新类别。',
    '样例使用新编的虚构聊天，不需要任何真实用户聊天。先扩充底层情绪/表达/意图/需求，再给合理组合。',
    '本文件是当前项目词库的导出，不会自动导入外部AI返回的新内容；扩充结果需再进行格式、层级和推理开销检查。',
    '',
    '来源：chatui/data/analysis-catalog.json；chatui/kaomoji.js；electron/laya/social-cues.ts；bridge/profile_signals.py。',
    f'主词库SHA-256：{hashlib.sha256(catalog_path.read_bytes()).hexdigest()}',
])
assert sum(item['expressionId'] in {row['id'] for row in catalog['expressions']} for item in catalog['playfulIntents']) == counts['playfulIntents']
args.output.parent.mkdir(parents=True, exist_ok=True)
if args.output.exists():
    raise SystemExit('Output already exists; choose another name rather than overwrite it')
args.output.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
print(json.dumps({'file':str(args.output),'counts':counts,'kaomojiSlots':sum(len(x['variants']) for x in faces.values()),
                  'bytes':args.output.stat().st_size,'lines':len(lines)},ensure_ascii=True))
