// Literal topic cues retrieve candidates; they never assign or suppress a label or probability.

const topics: Array<{ tokens: string[]; needs: string[] }> = [
  { tokens: ['抱抱', '抱我', '抱你', '拥抱'], needs: ['receive_closeness_hug', 'give_closeness_hug_other'] },
  { tokens: ['贴贴', '贴着', '依偎'], needs: ['receive_closeness_snuggle', 'give_closeness_snuggle_other'] },
  { tokens: ['摸头', '摸摸头', '摸我的头'], needs: ['receive_closeness_head_pat', 'give_closeness_pat_head_other'] },
  { tokens: ['牵手', '牵着', '牵我的手'], needs: ['receive_closeness_hand_hold', 'give_closeness_hold_hand_other'] },
  { tokens: ['妈妈', '妈咪'], needs: ['role_call_call_mom', 'role_call_hear_mom'] },
  { tokens: ['姐姐'], needs: ['role_call_call_sis', 'role_call_hear_sis'] },
  { tokens: ['老板'], needs: ['role_call_call_boss', 'role_call_hear_boss'] },
  { tokens: ['昵称', '外号'], needs: ['role_call_coin_nickname', 'role_call_hear_nickname'] },
  { tokens: ['哄哄', '哄我', '哄你'], needs: ['receive_words_be_coaxed', 'give_words_coax_other'] },
  { tokens: ['夸夸', '夸我', '夸你'], needs: ['receive_words_be_praised', 'give_words_praise_other'] },
  { tokens: ['晚安'], needs: ['receive_words_hear_goodnight', 'give_words_goodnight_other'] },
  { tokens: ['投喂', '带吃的'], needs: ['practical_care_receive_food'] },
  { tokens: ['一起玩', '陪我玩'], needs: ['shared_play_play_game'] },
  { tokens: ['一起散步'], needs: ['shared_play_walk_together'] },
  { tokens: ['一起吃饭'], needs: ['shared_play_eat_together'] },
];

export function mentionedSocialNeeds(text: string): string[] {
  return [...new Set(topics.filter(topic => topic.tokens.some(token => text.includes(token))).flatMap(topic => topic.needs))];
}
