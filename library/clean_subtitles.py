import re
from pathlib import Path


def read_file(_file_path):
    """
    a simple function that open a file in read mode
    :param _file_path: path to a certain file
    :return: opened file
    """
    data = Path(_file_path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass
    for encoding in ("utf-16", "cp1256", "cp1252", "iso-8859-1", "latin1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def save_file(_file_path, _content):
    """
    a function that replaces the content of the file with the new _content
    :param _file_path: path to a certain file
    :param _content: the content you want to write to the file
    :return: create or replace a file at the specified _file_path
    """
    with open(_file_path, "w", encoding="utf8") as _file_to_save:
        _file_to_save.write(str(_content))


def get_ads_list(_ads_file_path, ads_separator=","):
    """
    read and clean the ads file
    :param _ads_file_path: path to the ads file
    :return: a list of each ad
    """
    _ads_to_remove = read_file(_ads_file_path).split(ads_separator)
    _ads_to_remove = [ad.strip() for ad in _ads_to_remove]
    return _ads_to_remove


def clean_ads_regex(_subtitle_file_path, _ads_to_remove):
    full_path = Path(_subtitle_file_path)

    _content = read_file(full_path.absolute())
    # clean _ads_to_remove from empty strings
    _ads_to_remove = [ad for ad in _ads_to_remove if ad]

    # create a dynamic regex based on the start of each ad.
    # Ads are treated as literal text so user-supplied regex metacharacters cannot
    # crash the cleaner or cause catastrophic backtracking.
    regex_list = [re.escape(_ad) + r".*$" for _ad in _ads_to_remove if _ad]

    if not regex_list:
        return

    join_ads_regex = "|".join(regex_list)
    _file_content = re.sub(
        pattern=join_ads_regex,
        repl="",
        string=_content,
        flags=re.MULTILINE | re.IGNORECASE,
    )

    # result = re.findall(join_ads_regex, _text, re.MULTILINE)

    save_file(full_path.absolute(), _file_content)
    print(f"{full_path.absolute()} cleaned!")


def clean_ads(_subtitle_file_path, ads_separator=",", ads_file_path=None):
    """
    clean ads from a subtitle file
    :param _subtitle_file_path: path to the subtitle file
    :param _ads_file_path: path to the ads file
    :return: a new subtitle file without ads
    """
    if ads_file_path is None:
        current_script_directory = Path(__file__).parent.absolute()
        ads_file_path = Path(current_script_directory, "ads.txt")
    _ads_to_remove = get_ads_list(ads_file_path, ads_separator)
    clean_ads_regex(_subtitle_file_path, _ads_to_remove)
    return True


if __name__ == "__main__":
    print("This is a module to clean ads from subtitles.")
