"""Request-local cancellation and preview-only path caching; never persisted."""
import os
import stat


class PlanningCancelled(Exception):
    """Internal cooperative cancellation, not a validation failure."""


class PlanningContext:
    def __init__(self,cancelled=lambda:False,*,cache_paths=True):
        self.cancelled = cancelled
        self.cache_paths = cache_paths
        self.directories = {}

    def check(self):
        if self.cancelled():
            raise PlanningCancelled()

    def directory(self,path):
        self.check()
        if not self.cache_paths:
            return path.resolve()
        missing = []
        parent = path
        while parent not in self.directories:
            self.check()
            if parent.parent==parent:
                self.directories[parent] = parent.resolve()
                break
            missing.append(parent)
            parent = parent.parent
        for component in reversed(missing):
            self.check()
            self.directories[component] = self._leaf(component,self.directories[component.parent])
        return self.directories[path]

    def target(self,path):
        """Resolve shared parents once; inspect existing leaf reparse points too."""
        self.check()
        if not self.cache_paths:
            return path.resolve()
        parent = self.directory(path.parent)
        return self._leaf(path,parent)

    @staticmethod
    def _leaf(path,parent):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return parent/path.name
        reparse = getattr(info,'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)
        if stat.S_ISLNK(info.st_mode) or reparse:
            return path.resolve()
        return parent/path.name
