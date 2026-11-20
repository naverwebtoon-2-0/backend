package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestHealth(t *testing.T) {
	response := httptest.NewRecorder()
	routes().ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/healthz", nil))

	var result map[string]string
	if err := json.Unmarshal(response.Body.Bytes(), &result); err != nil {
		t.Fatal(err)
	}
	if response.Code != http.StatusOK || result["status"] != "ok" || result["revision"] != revision {
		t.Fatalf("상태 API 응답 오류: %d %v", response.Code, result)
	}
	if response.Header().Get("Cache-Control") != "no-store" {
		t.Fatal("상태 API는 캐시하면 안 된다")
	}
}
