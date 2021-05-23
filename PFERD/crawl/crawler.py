import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path, PurePath
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple, TypeVar

from rich.markup import escape

from ..auth import Authenticator
from ..config import Config, Section
from ..limiter import Limiter
from ..logging import ProgressBar, log
from ..output_dir import FileSink, FileSinkToken, OnConflict, OutputDirectory, OutputDirError, Redownload
from ..report import MarkConflictError, MarkDuplicateError, Report
from ..transformer import Transformer
from ..utils import ReusableAsyncContextManager, fmt_path


class CrawlWarning(Exception):
    pass


class CrawlError(Exception):
    pass


Wrapped = TypeVar("Wrapped", bound=Callable[..., None])


def noncritical(f: Wrapped) -> Wrapped:
    """
    Catches and logs a few noncritical exceptions occurring during the function
    call, mainly CrawlWarning.

    If any exception occurs during the function call, the crawler's error_free
    variable is set to False. This includes noncritical exceptions.

    Warning: Must only be applied to member functions of the Crawler class!
    """

    def wrapper(*args: Any, **kwargs: Any) -> None:
        if not (args and isinstance(args[0], Crawler)):
            raise RuntimeError("@noncritical must only applied to Crawler methods")

        crawler = args[0]

        try:
            f(*args, **kwargs)
        except (CrawlWarning, OutputDirError, MarkDuplicateError, MarkConflictError) as e:
            log.warn(str(e))
            crawler.error_free = False
        except:  # noqa: E722 do not use bare 'except'
            crawler.error_free = False
            raise

    return wrapper  # type: ignore


AWrapped = TypeVar("AWrapped", bound=Callable[..., Awaitable[None]])


def anoncritical(f: AWrapped) -> AWrapped:
    """
    An async version of @noncritical.

    Catches and logs a few noncritical exceptions occurring during the function
    call, mainly CrawlWarning.

    If any exception occurs during the function call, the crawler's error_free
    variable is set to False. This includes noncritical exceptions.

    Warning: Must only be applied to member functions of the Crawler class!
    """

    async def wrapper(*args: Any, **kwargs: Any) -> None:
        if not (args and isinstance(args[0], Crawler)):
            raise RuntimeError("@anoncritical must only applied to Crawler methods")

        crawler = args[0]

        try:
            await f(*args, **kwargs)
        except (CrawlWarning, OutputDirError, MarkDuplicateError, MarkConflictError) as e:
            log.warn(str(e))
            crawler.error_free = False
        except:  # noqa: E722 do not use bare 'except'
            crawler.error_free = False
            raise

    return wrapper  # type: ignore


class CrawlToken(ReusableAsyncContextManager[ProgressBar]):
    def __init__(self, limiter: Limiter, path: PurePath):
        super().__init__()

        self._limiter = limiter
        self._path = path

    async def _on_aenter(self) -> ProgressBar:
        bar_desc = f"[bold bright_cyan]Crawling[/] {escape(fmt_path(self._path))}"
        after_desc = f"[bold cyan]Crawled[/] {escape(fmt_path(self._path))}"

        self._stack.callback(lambda: log.action(after_desc))
        await self._stack.enter_async_context(self._limiter.limit_crawl())
        bar = self._stack.enter_context(log.crawl_bar(bar_desc))

        return bar


class DownloadToken(ReusableAsyncContextManager[Tuple[ProgressBar, FileSink]]):
    def __init__(self, limiter: Limiter, fs_token: FileSinkToken, path: PurePath):
        super().__init__()

        self._limiter = limiter
        self._fs_token = fs_token
        self._path = path

    async def _on_aenter(self) -> Tuple[ProgressBar, FileSink]:
        bar_desc = f"[bold bright_cyan]Downloading[/] {escape(fmt_path(self._path))}"
        # The "Downloaded ..." message is printed in the output dir, not here

        await self._stack.enter_async_context(self._limiter.limit_download())
        sink = await self._stack.enter_async_context(self._fs_token)
        bar = self._stack.enter_context(log.download_bar(bar_desc))

        return bar, sink


class CrawlerSection(Section):
    def output_dir(self, name: str) -> Path:
        # TODO Use removeprefix() after switching to 3.9
        if name.startswith("crawl:"):
            name = name[len("crawl:"):]
        return Path(self.s.get("output_dir", name)).expanduser()

    def redownload(self) -> Redownload:
        value = self.s.get("redownload", "never-smart")
        try:
            return Redownload.from_string(value)
        except ValueError as e:
            self.invalid_value(
                "redownload",
                value,
                str(e).capitalize(),
            )

    def on_conflict(self) -> OnConflict:
        value = self.s.get("on_conflict", "prompt")
        try:
            return OnConflict.from_string(value)
        except ValueError as e:
            self.invalid_value(
                "on_conflict",
                value,
                str(e).capitalize(),
            )

    def transform(self) -> str:
        return self.s.get("transform", "")

    def max_concurrent_tasks(self) -> int:
        value = self.s.getint("max_concurrent_tasks", fallback=1)
        if value <= 0:
            self.invalid_value("max_concurrent_tasks", value,
                               "Must be greater than 0")
        return value

    def max_concurrent_downloads(self) -> int:
        tasks = self.max_concurrent_tasks()
        value = self.s.getint("max_concurrent_downloads", fallback=None)
        if value is None:
            return tasks
        if value <= 0:
            self.invalid_value("max_concurrent_downloads", value,
                               "Must be greater than 0")
        if value > tasks:
            self.invalid_value("max_concurrent_downloads", value,
                               "Must not be greater than max_concurrent_tasks")
        return value

    def delay_between_tasks(self) -> float:
        value = self.s.getfloat("delay_between_tasks", fallback=0.0)
        if value < 0:
            self.invalid_value("delay_between_tasks", value,
                               "Must not be negative")
        return value

    def auth(self, authenticators: Dict[str, Authenticator]) -> Authenticator:
        value = self.s.get("auth")
        if value is None:
            self.missing_value("auth")
        auth = authenticators.get(value)
        if auth is None:
            self.invalid_value("auth", value, "No such auth section exists")
        return auth


class Crawler(ABC):
    def __init__(
            self,
            name: str,
            section: CrawlerSection,
            config: Config,
    ) -> None:
        """
        Initialize a crawler from its name and its section in the config file.

        If you are writing your own constructor for your own crawler, make sure
        to call this constructor first (via super().__init__).

        May throw a CrawlerLoadException.
        """

        self.name = name
        self.error_free = True

        self._limiter = Limiter(
            task_limit=section.max_concurrent_tasks(),
            download_limit=section.max_concurrent_downloads(),
            task_delay=section.delay_between_tasks(),
        )

        self._transformer = Transformer(section.transform())

        self._output_dir = OutputDirectory(
            config.default_section.working_dir() / section.output_dir(name),
            section.redownload(),
            section.on_conflict(),
        )

    @property
    def report(self) -> Report:
        return self._output_dir.report

    @property
    def prev_report(self) -> Optional[Report]:
        return self._output_dir.prev_report

    @staticmethod
    async def gather(awaitables: Sequence[Awaitable[Any]]) -> List[Any]:
        """
        Similar to asyncio.gather. However, in the case of an exception, all
        still running tasks are cancelled and the exception is rethrown.

        This should always be preferred over asyncio.gather in crawler code so
        that an exception like CrawlError may actually stop the crawler.
        """

        tasks = [asyncio.ensure_future(aw) for aw in awaitables]
        result = asyncio.gather(*tasks)
        try:
            return await result
        except:  # noqa: E722
            for task in tasks:
                task.cancel()
            raise

    async def crawl(self, path: PurePath) -> Optional[CrawlToken]:
        log.explain_topic(f"Decision: Crawl {fmt_path(path)}")

        if self._transformer.transform(path) is None:
            log.explain("Answer: No")
            return None

        log.explain("Answer: Yes")
        return CrawlToken(self._limiter, path)

    async def download(
            self,
            path: PurePath,
            mtime: Optional[datetime] = None,
            redownload: Optional[Redownload] = None,
            on_conflict: Optional[OnConflict] = None,
    ) -> Optional[DownloadToken]:
        log.explain_topic(f"Decision: Download {fmt_path(path)}")

        transformed_path = self._transformer.transform(path)
        if transformed_path is None:
            log.explain("Answer: No")
            return None

        fs_token = await self._output_dir.download(path, transformed_path, mtime, redownload, on_conflict)
        if fs_token is None:
            log.explain("Answer: No")
            return None

        log.explain("Answer: Yes")
        return DownloadToken(self._limiter, fs_token, path)

    async def _cleanup(self) -> None:
        log.explain_topic("Decision: Clean up files")
        if self.error_free:
            log.explain("No warnings or errors occurred during this run")
            log.explain("Answer: Yes")
            await self._output_dir.cleanup()
        else:
            log.explain("Warnings or errors occurred during this run")
            log.explain("Answer: No")

    async def run(self) -> None:
        """
        Start the crawling process. Call this function if you want to use a
        crawler.
        """

        with log.show_progress():
            self._output_dir.prepare()
            self._output_dir.load_prev_report()
            await self._run()
            await self._cleanup()
            self._output_dir.store_report()

    @abstractmethod
    async def _run(self) -> None:
        """
        Overwrite this function if you are writing a crawler.

        This function must not return before all crawling is complete. To crawl
        multiple things concurrently, asyncio.gather can be used.
        """

        pass