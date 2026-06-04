# target.py

class TaskQueue:
    def __init__(self):
        self.tasks = []

    def add_task(self, task_name, dependencies=None):
        """
        Add a task with optional dependencies.
        """
        if dependencies is None:
            dependencies = []
        self.tasks.append((task_name, dependencies))
        return self.tasks

    def get_tasks(self):
        """
        Retrieve the current list of tasks.
        """
        return self.tasks

    def clear_tasks(self):
        """
        Clear all tasks from the queue.
        """
        self.tasks = []