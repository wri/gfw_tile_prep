from pydantic import BaseSettings


class EnvSettings(BaseSettings):
    def env_dict(self):
        env = self.dict(exclude_none=True)
        return {key.upper(): str(value) for key, value in env.items()}

    class Config:
        case_sensitive = False
        validate_assignment = True
