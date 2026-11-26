import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(prog="jatai", description="Jataí — research agent")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="Inicia o servidor web local")
    start.add_argument("--host", default="127.0.0.1")
    start.add_argument("--port", type=int, default=8000)

    new = sub.add_parser("new", help="Cria um novo projeto")
    new.add_argument("name")
    new.add_argument("--description", default="")

    sub.add_parser("init", help="Cria a estrutura base em ~/.jatai")

    login = sub.add_parser(
        "pdf-login",
        help="Abre o navegador para login institucional (sessão "
             "autenticada usada pela aquisição de PDFs)")
    login.add_argument("--url", default="https://www.google.com",
                       help="URL inicial no navegador")

    args = parser.parse_args()

    if args.command == "start":
        import uvicorn

        from jatai.app import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.command == "new":
        from jatai.projects.service import create_project

        project = create_project(args.name, args.description)
        print(f"Projeto criado: {project.name} ({project.slug})")
        print(f"Workspace: {project.workspace}")
    elif args.command == "init":
        from jatai.config.settings import ensure_dirs

        print(f"Diretório base: {ensure_dirs()}")
    elif args.command == "pdf-login":
        from jatai.documents.acquisition import (
            auth_profile_dir,
            open_login_window,
        )

        print(f"Perfil: {auth_profile_dir()}")
        print("Faça login nas plataformas institucionais e feche a "
              "janela quando terminar.")
        open_login_window(args.url)
    else:
        sys.exit(2)


if __name__ == "__main__":
    main()
