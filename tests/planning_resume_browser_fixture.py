"""Synthetic app for planning recovery browser checks; requires isolated schema."""
import json
import os
from time import sleep
from sqlalchemy.engine import make_url
from app.logging_audit.db import Database
from app.slice3_app import Slice3State, create_slice3_app
from app.planning.models import LearningRouteRow, PlanningTaskStateRow
from app.slice1.errors import AdapterError
from fastapi.middleware.cors import CORSMiddleware
from test_v2_intake import InterviewAdapter
from common_fixtures import SyntheticSearchAdapter

assert 'planning_resume_browser_' in make_url(os.environ['LEARNING_DATABASE_URL']).query.get('options','')
db=Database()
models={}


class BrowserAdapter(InterviewAdapter):
    def __init__(self, title):
        super().__init__()
        self.title=title
        self.failed=False

    def generate(self,messages,**options):
        functions=[item['function'] for item in options.get('tools',[])]
        interview=any('ready_to_plan' in f.get('parameters',{}).get('properties',{}) for f in functions)
        if interview:
            context=next(json.loads(m['content']) for m in messages if m['role']=='user' and m['content'].startswith('{'))
            if context['shown_count']:
                if '失败' in self.title and not self.failed:
                    self.failed=True
                    raise AdapterError('capability_test_failed',status=502)
                if '延迟' in self.title:sleep(4)
        return super().generate(messages,**options)


def model_factory(owner,task):
    if task not in models:
        with db.session() as sess:
            state=sess.get(PlanningTaskStateRow,task)
            route=sess.get(LearningRouteRow,state.target_id)
            title=route.title_ref if route else '合成规划'
        models[task]=BrowserAdapter(title)
    return models[task]


state=Slice3State(db,model_adapter_factory=model_factory,search_adapter_factory=lambda *args:SyntheticSearchAdapter())
db.create_all()
app=create_slice3_app(state)
app.add_middleware(CORSMiddleware,allow_origins=['http://localhost:5175','http://127.0.0.1:5175'],allow_credentials=True,allow_methods=['*'],allow_headers=['*'])


@app.on_event('shutdown')
def stop():state.planning.background.close()
