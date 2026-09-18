import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { applyPaletteCssVariables } from "./palette";
import "./styles.css";

applyPaletteCssVariables(document.documentElement);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
