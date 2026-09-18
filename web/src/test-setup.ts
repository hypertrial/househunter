import "@testing-library/jest-dom/vitest";
import { applyPaletteCssVariables } from "./palette";

applyPaletteCssVariables(document.documentElement);

if (typeof HTMLDialogElement !== "undefined") {
  HTMLDialogElement.prototype.showModal = function showModal() {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function close() {
    this.removeAttribute("open");
  };
}
