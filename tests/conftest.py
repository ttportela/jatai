import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def jatai_home(tmp_path_factory):
    home = tmp_path_factory.mktemp("jatai_home")
    os.environ["JATAI_HOME"] = str(home)
    return home


@pytest.fixture()
def slug(jatai_home):
    """Projeto novo e isolado por teste."""
    from jatai.projects import service as projects
    from jatai.research import service as research
    from jatai.storage.db import get_project_session
    from jatai.storage.models_project import ProjectSource

    p = projects.create_project("Teste Agentes")
    s = p.slug
    # desativa fontes reais e registra uma fonte fake para os testes
    for src in research.list_sources(s):
        research.set_source_enabled(s, src.id, False)
    with get_project_session(s) as sess:
        sess.add(ProjectSource(source_type="fake", name="Fake", enabled=True, config={}))
        sess.commit()
    return s
