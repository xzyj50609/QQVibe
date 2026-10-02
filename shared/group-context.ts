/** Explicit group roles. Identifiers are bounded local pseudonyms, never names. */
export interface GroupContext {
  speaker: string;
  time: number;
  quote: {speaker: string | null; text: string} | null;
  mentions: Array<{speaker: string | null; kind: 'user' | 'all'}>;
}

function speaker(value: unknown): string {
  if(typeof value!=='string' || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/u.test(value)) throw new Error('invalid group speaker');
  return value;
}
export function checkedGroupContext(value: unknown): GroupContext {
  if(!value || typeof value!=='object' || Array.isArray(value)) throw new Error('group context required');
  const raw=value as Record<string,unknown>;
  if(typeof raw.time!=='number' || !Number.isSafeInteger(raw.time) || raw.time<0 || !Array.isArray(raw.mentions) || raw.mentions.length>64) throw new Error('invalid group context');
  let quote: GroupContext['quote']=null;
  if(raw.quote!=null) {
    if(typeof raw.quote!=='object' || Array.isArray(raw.quote)) throw new Error('invalid group quote');
    const q=raw.quote as Record<string,unknown>;
    if(typeof q.text!=='string' || Array.from(q.text).length>4000) throw new Error('invalid group quote');
    quote={speaker:q.speaker==null?null:speaker(q.speaker),text:q.text};
  }
  const mentions=raw.mentions.map(value=>{
    if(!value || typeof value!=='object' || !['user','all'].includes(value.kind)) throw new Error('invalid group mention');
    return {speaker:value.speaker==null?null:speaker(value.speaker),kind:value.kind as 'user'|'all'};
  });
  return {speaker:speaker(raw.speaker),time:raw.time,quote,mentions};
}

export const GROUP_ROLE_VERSION='qq-group-role-v1';
export const GROUP_NEIGHBOR_MS=10*60*1000;
export interface GroupRoleMessage {side: 'self'|'other';text: string;groupContext?: GroupContext;}
export function groupTargetState(messages: readonly GroupRoleMessage[],index:number): string {
  const target=messages[index]!;
  const role=checkedGroupContext(target.groupContext);
  const background=messages.slice(Math.max(0,index-3),index).filter(item=>item.groupContext &&
    role.time-item.groupContext.time>=0 && role.time-item.groupContext.time<=GROUP_NEIGHBOR_MS);
  return 'QQ_GROUP_TARGET_V1\n'+JSON.stringify({message:target.text,target:{role:'TARGET',speaker:role.speaker,side:target.side},
    kind:'group',policy:GROUP_ROLE_VERSION,
    background:background.filter(item=>!role.quote || role.quote.speaker!==item.groupContext!.speaker || role.quote.text!==item.text)
      .map(item=>({role:'BACKGROUND',speaker:item.groupContext!.speaker,side:item.side,text:item.text})),
    quote:role.quote?{role:'QUOTED',...role.quote}:null,mentions:role.mentions,
    instructions:'Judge only message by TARGET speaker. Background and quote are not TARGET evidence. Mentions do not prove an addressee.'});
}

export function fitGroupState(state:string,max:number,count:(value:string)=>number):string {
  if(!state.startsWith('QQ_GROUP_TARGET_V1\n')) return state;
  const raw=JSON.parse(state.slice('QQ_GROUP_TARGET_V1\n'.length));
  const render=()=> 'QQ_GROUP_TARGET_V1\n'+JSON.stringify(raw);
  while(raw.background?.length && count(render())>max) raw.background.shift();
  if(count(render())>max && raw.quote?.text) raw.quote={role:'QUOTED',speaker:raw.quote.speaker,omitted:true};
  return render();
}
