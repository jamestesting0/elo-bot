import asyncio
import traceback
from functools import wraps

import interactions


def lock_files(*file_paths):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            # acquire each lock asynchronously
            if isinstance(self, interactions.Client):
                file_locks = self.file_locks
            else:
                file_locks = self.bot.file_locks
            locks = [file_locks[file_path] for file_path in sorted(file_paths)]
            for lock in locks:
                await lock.acquire()

            try:
                return await func(self, *args, **kwargs)  # call the original function
            finally:
                for lock in locks:
                    lock.release()

        return wrapper

    return decorator


def create_async_task(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        loop = asyncio.get_running_loop()
        return loop.create_task(func(*args, **kwargs))

    return wrapper


def reset_cooldown_on_error(command_func):
    @wraps(command_func)
    async def wrapper(self, ctx: interactions.SlashContext, *args, **kwargs):
        try:
            return await command_func(self, ctx, *args, **kwargs)
        except Exception:
            try:
                await command_func.cooldown.reset(ctx)
            except Exception:
                pass  # ignore if reset fails

            traceback.print_exc()
            raise
    return wrapper
