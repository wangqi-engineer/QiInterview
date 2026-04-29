import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import "./index.css";
import { AppShell } from "./components/AppShell";
import { RequireAuth } from "./components/RequireAuth";
import SetupPage from "./pages/SetupPage";
import InterviewPage from "./pages/InterviewPage";
import ReportPage from "./pages/ReportPage";
import HistoryPage from "./pages/HistoryPage";
import LoginPage from "./pages/LoginPage";
import RegisterPage from "./pages/RegisterPage";
import ForgotPasswordPage from "./pages/ForgotPasswordPage";
import ResetPasswordPage from "./pages/ResetPasswordPage";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <BrowserRouter>
      <AppShell>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/forgot-password" element={<ForgotPasswordPage />} />
          <Route path="/reset-password" element={<ResetPasswordPage />} />
          <Route path="/" element={<Navigate to="/setup" replace />} />
          <Route
            path="/setup"
            element={
              <RequireAuth>
                <SetupPage />
              </RequireAuth>
            }
          />
          <Route
            path="/interview/:sid"
            element={
              <RequireAuth>
                <InterviewPage />
              </RequireAuth>
            }
          />
          <Route
            path="/report/:sid"
            element={
              <RequireAuth>
                <ReportPage />
              </RequireAuth>
            }
          />
          <Route
            path="/history"
            element={
              <RequireAuth>
                <HistoryPage />
              </RequireAuth>
            }
          />
          <Route path="*" element={<Navigate to="/setup" replace />} />
        </Routes>
      </AppShell>
    </BrowserRouter>
  </React.StrictMode>,
);
