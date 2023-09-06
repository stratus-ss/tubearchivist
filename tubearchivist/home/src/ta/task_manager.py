"""
functionality:
- interact with in redis stored task results
- handle threads and locks
"""

from home import tasks as ta_tasks
from home.src.ta.ta_redis import RedisArchivist, TaskRedis


class TaskManager:
    """manage tasks"""

    def get_all_results(self):
        """return all task results"""
        handler = TaskRedis()
        all_keys = handler.get_all()
        return False if not all_keys else [handler.get_single(i) for i in all_keys]

    def get_tasks_by_name(self, task_name):
        """get all tasks by name"""
        if all_results := self.get_all_results():
            return [i for i in all_results if i.get("name") == task_name]
        else:
            return False

    def get_task(self, task_id):
        """get single task"""
        return TaskRedis().get_single(task_id)

    def is_pending(self, task):
        """check if task_name is pending, pass task object"""
        if tasks := self.get_tasks_by_name(task.name):
            return bool([i for i in tasks if i.get("status") == "PENDING"])
        else:
            return False

    def is_stopped(self, task_id):
        """check if task_id has received STOP command"""
        task = self.get_task(task_id)

        return task.get("command") == "STOP"

    def get_pending(self, task_name):
        """get all pending tasks of task_name"""
        if tasks := self.get_tasks_by_name(task_name):
            return [i for i in tasks if i.get("status") == "PENDING"]
        else:
            return False

    def init(self, task):
        """pass task object from bind task to set initial pending message"""
        message = {
            "status": "PENDING",
            "result": None,
            "traceback": None,
            "date_done": False,
            "name": task.name,
            "task_id": task.request.id,
        }
        TaskRedis().set_key(task.request.id, message)

    def fail_pending(self):
        """
        mark all pending as failed,
        run at startup to recover from hard reset
        """
        all_results = self.get_all_results()
        if not all_results:
            return

        for result in all_results:
            if result.get("status") == "PENDING":
                result["status"] = "FAILED"
                TaskRedis().set_key(result["task_id"], result, expire=True)


class TaskCommand:
    """run commands on task"""

    def start(self, task_name):
        """start task by task_name, only pass task that don't take args"""
        task = ta_tasks.app.tasks.get(task_name).delay()
        return {
            "task_id": task.id,
            "status": task.status,
            "task_name": task.name,
        }

    def stop(self, task_id, message_key):
        """
        send stop signal to task_id,
        needs to be implemented in task to take effect
        """
        print(f"[task][{task_id}]: received STOP signal.")
        handler = TaskRedis()

        task = handler.get_single(task_id)
        if task["name"] not in ta_tasks.BaseTask.TASK_CONFIG:
            raise ValueError

        handler.set_command(task_id, "STOP")
        RedisArchivist().set_message(message_key, "STOP", path=".command")

    def kill(self, task_id):
        """send kill signal to task_id"""
        print(f"[task][{task_id}]: received KILL signal.")
        ta_tasks.app.control.revoke(task_id, terminate=True)
