import subprocess
import os
from typing import List, Optional

def get_git_files() -> List[str]:
    """Получает список файлов, учитывая .gitignore."""
    try:
        # -z использует нулевой байт как разделитель (безопасно для пробелов)
        # --cached (отслеживаемые) + --others (новые) --exclude-standard (правила ignore)
        cmd = ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        # Декодируем и убираем пустой элемент в конце, если есть
        return [f for f in res.stdout.decode('utf-8').split('\0') if f]
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("Ошибка: Не найден git репозиторий или git не установлен.")
        return []

def read_file_content(filepath: str) -> Optional[str]:
    """Читает текстовый файл, пропуская бинарники."""
    if not os.path.isfile(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        return None  # Пропускаем бинарные файлы (картинки, exe и т.д.)

def save_bundle(files: List[str], output_path: str) -> None:
    """Записывает пути и содержимое файлов в итоговый файл."""
    with open(output_path, 'w', encoding='utf-8') as out:
        for path in files:
            content = read_file_content(path)
            if content is not None:
                header = f"\n{'='*40}\nFILE: {path}\n{'='*40}\n"
                out.write(f"{header}{content}\n")

def main() -> None:
    files = get_git_files()
    if not files:
        return

    output_file = "project_bundle.txt"
    save_bundle(files, output_file)
    print(f"Успешно упаковано {len(files)} файлов в {output_file}")

if __name__ == "__main__":
    main()
