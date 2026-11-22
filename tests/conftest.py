import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def lelia_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("lelia_home")
    os.environ["LELIA_HOME"] = str(home)
    return home


@pytest.fixture()
def slug(lelia_home):
    """Projeto novo e isolado por teste."""
    from lelia.projects import service as projects
    from lelia.research import service as research
    from lelia.storage.db import get_project_session
    from lelia.storage.models_project import ProjectSource

    p = projects.create_project("Teste Agentes")
    s = p.slug
    # desativa fontes reais e registra uma fonte fake para os testes
    for src in research.list_sources(s):
        research.set_source_enabled(s, src.id, False)
    with get_project_session(s) as sess:
        sess.add(ProjectSource(source_type="fake", name="Fake", enabled=True, config={}))
        sess.commit()
    return s
