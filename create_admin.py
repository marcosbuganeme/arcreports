import sys
import os

# Adicionar a raiz do projeto ao PATH do Python
sys.path.insert(0, '/opt/sites/arcreports')

try:
    from app.security import hash_password
    from app.db import engine  # ou import via mysql.connector
    import mysql.connector
    from dotenv import load_dotenv
except ImportError as e:
    print(f"[ERRO] Falha ao importar dependências: {e}")
    sys.exit(1)

load_dotenv('/opt/sites/arcreports/.env')

def main():
    print("=== Criador de Administrador ArcReports ===")
    username = input("Usuário [admin]: ").strip() or "admin"
    email = input("E-mail [admin@detran.go.gov.br]: ").strip() or "admin@detran.go.gov.br"
    password = input("Senha do Admin: ").strip()

    if not password:
        print("[ERRO] Senha obrigatória.")
        sys.exit(1)

    # Gerar o hash usando exatamente a função nativa do ArcReports
    pwd_hash = hash_password(password)

    db_host = os.getenv("MYSQL_HOST") or os.getenv("DB_HOST") or "127.0.0.1"
    db_user = os.getenv("MYSQL_USER") or os.getenv("DB_USER") or "usr_arcreports"
    db_pass = os.getenv("MYSQL_PASSWORD") or os.getenv("DB_PASSWORD") or ""
    db_name = os.getenv("MYSQL_DATABASE") or os.getenv("DB_NAME") or "arcreports"
    db_port = int(os.getenv("MYSQL_PORT") or os.getenv("DB_PORT") or 3306)

    try:
        conn = mysql.connector.connect(
            host=db_host, user=db_user, password=db_pass, database=db_name, port=db_port
        )
        cursor = conn.cursor()

        sql = """
            INSERT INTO users (username, email, password_hash, is_admin, is_active)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                email = VALUES(email),
                password_hash = VALUES(password_hash),
                is_admin = VALUES(is_admin),
                is_active = VALUES(is_active);
        """
        cursor.execute(sql, (username, email, pwd_hash, True, True))
        conn.commit()
        print(f"\n[SUCESSO] Administrador '{username}' criado/atualizado com sucesso!")

    except Exception as err:
        print(f"\n[ERRO no MariaDB]: {err}")
    finally:
        if 'conn' in locals() and conn.is_connected():
            cursor.close()
            conn.close()

if __name__ == "__main__":
    main()
