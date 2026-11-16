"""Local policy component. NOT an authenticated network service.
Principal is supplied only by a trusted adapter; never construct it from model arguments.
Current OpenClaw stdio has no such adapter: member transport must remain disabled.
"""
import json,hashlib
from dataclasses import dataclass
@dataclass(frozen=True)
class Principal:
    subject: str
    role: str
    trusted: bool = False
class Policy:
    def __init__(self, registry):
        self.registry=registry;self.epoch=registry['version'];self.cache={};self.audit=[]
        self.records={fid:mid for mid,r in registry['meetings'].items() for fid in r['record_file_ids']}
    def allow(self,principal,meeting_id=None,record_file_id=None,sub_meeting_id=None,**untrusted):
        mid=str(meeting_id) if meeting_id is not None else self.records.get(str(record_file_id))
        # An unowned recording cannot be opened by members, even if another meeting ID was supplied.
        if record_file_id is not None:
            owner=self.records.get(str(record_file_id))
            if not owner or (meeting_id is not None and owner!=str(meeting_id)):mid=None
            else:mid=owner
        ok=False
        if principal.trusted:
            if principal.role=='owner':ok=True
            elif principal.role=='member' and mid:
                entry=self.registry['meetings'].get(mid,{})
                ok=entry.get('mark')=='company'
                if sub_meeting_id is not None and str(sub_meeting_id) not in entry.get('sub_meeting_ids',[]):ok=False
        self.audit.append({'subject':principal.subject,'role':principal.role,'mid':mid,'allow':ok,'version':self.epoch})
        return ok
    def filter(self,p,rows):
        return [r for r in rows if self.allow(p,r.get('meeting_id'),r.get('record_file_id'))]
    def page(self,p,rows,page=0,size=30):
        # Synthetic local pagination AFTER filtering. Never forward upstream totals/tokens/snippets.
        visible=self.filter(p,rows);start=page*size
        return {'items':visible[start:start+size],'has_more':start+size<len(visible),'visible_total':len(visible)}
    def cache_summary(self,p,key,source_ids,text):
        if not source_ids or not all(self.allow(p,m) for m in source_ids):return False
        self.cache[(p.subject,self.epoch,key)]={'sources':tuple(source_ids),'text':text};return True
    def get_summary(self,p,key):
        item=self.cache.get((p.subject,self.epoch,key))
        if not item or not all(self.allow(p,m) for m in item['sources']):return None
        return item['text']
    def revoke(self,mid):
        self.registry['meetings'][mid]['mark']='pending';self.epoch+=1
        self.registry['version']=self.epoch;self.cache.clear()
