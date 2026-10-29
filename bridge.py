"""CLI de administração local; não expõe rede."""
import argparse
from config import DB_PATH
from database import initialize, create_self_test, list_tasks, get_task

def main():
    parser = argparse.ArgumentParser(description="LUK Codex Bridge — protótipo local")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="Inicializa o banco SQLite")
    rec = sub.add_parser(
        "reconcile-uncertain",
        help="Reconcilia tarefa incerta após revisão manual"
    )
    rec.add_argument("task_id")
    rec.add_argument("--reason", required=True)
    rec.add_argument(
        "--confirm",
        required=True,
        choices=["CONFIRMO_REVISAO_DOS_EFEITOS"]
    )
    sub.add_parser("submit-self-test", help="Enfileira uma tarefa fixa de teste")
    ls = sub.add_parser("list", help="Lista tarefas")
    ls.add_argument("--limit", type=int, default=20)
    show = sub.add_parser("show", help="Mostra uma tarefa")
    show.add_argument("task_id")
    args = parser.parse_args()
    if args.command == "reconcile-uncertain":
        from database import reconcile_uncertain
        reconcile_uncertain(
            DB_PATH, args.task_id, args.reason, args.confirm
        )
        print("Tarefa reconciliada; histórico preservado.")
        return


    if args.command == "init":
        initialize(DB_PATH)
        print(f"Banco inicializado: {DB_PATH}")
    elif args.command == "submit-self-test":
        initialize(DB_PATH)
        print(create_self_test(DB_PATH))
    elif args.command == "list":
        initialize(DB_PATH)
        for row in list_tasks(DB_PATH, max(1, min(args.limit, 100))):
            print(f"{row['id']}  {row['status']}  {row['action']}  {row['created_at']}")
    elif args.command == "show":
        initialize(DB_PATH)
        row = get_task(DB_PATH, args.task_id)
        if row is None:
            print("Tarefa não encontrada")
            raise SystemExit(1)
        for key in row.keys():
            if key in {"result", "error"}:
                print(f"{key}: {'present (omitted)' if row[key] is not None else 'none'}")
            else:
                print(f"{key}: {row[key]}")

if __name__ == "__main__":
    main()
