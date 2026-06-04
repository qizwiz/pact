# test_target.py

import target

def test_task_queue_dependencies():
    queue = target.TaskQueue()
    
    # First call: add a task with no dependencies
    queue.add_task("Task1")
    
    # Second call: add a task with dependencies
    queue.add_task("Task2", ["Task1"])
    
    tasks = queue.get_tasks()
    assert tasks == [("Task1", []), ("Task2", ["Task1"])], "Tasks should not share dependencies across calls"