"""Own preview invalidation, request state and revision-safe result application."""
import json

from sortzip_core.planning import StepError
from sortzip_core.workspace_state import PreviewResult


class PreviewCoordinator:
    def __init__(self,window):
        self.window = window
        self.cache = None
        self.submitted_revision = None
        self.plan_signature = None

    def signature(self):
        state = self.window.state
        fields = ('source','destination','mappings','mapping_enabled','rules','rule_enabled','recursive',
            'keep_hierarchy','output_list','compression_enabled','sort_by','preview_order','archive_tool')
        data = {key:getattr(state,key) for key in fields}
        data['layers'] = [{key:value for key,value in draft.data.items()
            if key not in ('password','confirm','encrypt','verify','expanded','show_password')} for draft in state.layers]
        return json.dumps(data,sort_keys=True,ensure_ascii=False)

    def loaded(self):
        self.plan_signature = self.signature()

    def changed(self,previous_revision,previous_status):
        w = self.window
        signature = self.signature()
        if signature==self.plan_signature:
            w.state.revision = previous_revision
            w.timer.stop()
            w.preview_status.setText(previous_status)
            self.refresh()
        else:
            self.mark_pending()
        self.plan_signature = signature

    def invalidate(self):
        self.cache = None
        self.submitted_revision = None

    def mark_pending(self):
        w = self.window
        w._preview_has_issue = True
        w.preview_status.setText('正在更新预览…')
        w.sync_preview_status()
        w.review_btn.setEnabled(False)

    def refresh(self):
        w = self.window
        w.timer.stop()
        if w._closing:
            return
        if w.state.preview is not None and w.state.preview.revision==w.state.revision:
            w._render_preview()
        if w._task_busy() or w._resume_saved_state is not None or self.submitted_revision==w.state.revision:
            return
        self.submitted_revision = w.state.revision
        self.mark_pending()
        try:
            config = w.state.to_config(False)
            config['_preview_cache'] = self.cache
        except (ValueError,TypeError) as error:
            result = PreviewResult(w.state.revision,file_error=str(error))
            if isinstance(error,StepError): result.node_errors[error.node] = str(error)
            w.state.preview = result
            w._render_preview()
            return
        w.preview_service.submit(config,w.state.revision)

    def accept(self,result):
        w = self.window
        if result is None or w._closing or result.revision!=w.state.revision:
            return False
        w.state.preview = result
        if result.inventory is not None:
            self.cache = result
        w._render_preview()
        return True
