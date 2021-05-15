from typing import Optional, Tuple

from ..authenticator import Authenticator, AuthException, AuthSection
from ..conductor import TerminalConductor
from ..config import Config
from ..utils import agetpass, ainput


class SimpleAuthSection(AuthSection):
    def username(self) -> Optional[str]:
        return self.s.get("username")

    def password(self) -> Optional[str]:
        return self.s.get("password")


class SimpleAuthenticator(Authenticator):
    def __init__(
            self,
            name: str,
            section: SimpleAuthSection,
            config: Config,
            conductor: TerminalConductor,
    ) -> None:
        super().__init__(name, section, config, conductor)

        self._username = section.username()
        self._password = section.password()

        self._username_fixed = self.username is not None
        self._password_fixed = self.password is not None

    async def credentials(self) -> Tuple[str, str]:
        if self._username is not None and self._password is not None:
            return self._username, self._password

        async with self.conductor.exclusive_output():
            if self._username is None:
                self._username = await ainput("Username: ")
            else:
                print(f"Username: {self.username}")

            if self._password is None:
                self._password = await agetpass("Password: ")

            # Intentionally returned inside the context manager so we know
            # they're both not None
            return self._username, self._password

    def invalidate_credentials(self) -> None:
        if self._username_fixed and self._password_fixed:
            raise AuthException("Configured credentials are invalid")

        if not self._username_fixed:
            self._username = None
        if not self._password_fixed:
            self._password = None

    def invalidate_username(self) -> None:
        if self._username_fixed:
            raise AuthException("Configured username is invalid")
        else:
            self._username = None

    def invalidate_password(self) -> None:
        if self._password_fixed:
            raise AuthException("Configured password is invalid")
        else:
            self._password = None
