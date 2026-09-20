from sqlalchemy import select

from app.models.user import User
from app.repositories.base import TenantScopedRepository


class UserRepository(TenantScopedRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        """
        Utilisé uniquement à la connexion, avant qu'un tenant_id ne soit connu :
        le login se fait par email GLOBAL, mais un même email ne peut exister
        que pour un seul (tenant, email) grâce à la contrainte unique en base.
        """
        stmt = select(User).where(User.email == email, User.active.is_(True))
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()
