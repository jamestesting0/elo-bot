from dataclasses import dataclass

import interactions as ipy


@dataclass(frozen=True, slots=True)
class InvalidPageError(Exception):
    message: str

    def __str__(self) -> str:
        return self.message


def build_page_button(current_page: int, total_pages: int, custom_id: str) -> ipy.Button:
    return ipy.Button(
        style=ipy.ButtonStyle.SECONDARY,
        label=f"{current_page}/{total_pages}",
        custom_id=custom_id,
        disabled=False,
    )


def build_page_jump_modal(custom_id: str) -> ipy.Modal:
    return ipy.Modal(
        ipy.ShortText(
            label="Page number",
            custom_id="page",
            required=True,
        ),
        title="Jump to page",
        custom_id=custom_id,
    )


def parse_page_number(value: str, total_pages: int) -> int:
    normalized_value = value.strip()
    if not normalized_value.isdigit():
        raise InvalidPageError("Page must be a positive integer.")

    page = int(normalized_value)
    if page < 1:
        raise InvalidPageError("Page must be a positive integer.")
    if page > total_pages:
        raise InvalidPageError(f"Page must be between 1 and {total_pages}.")

    return page
