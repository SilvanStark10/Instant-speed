package main

import (
	"context"
	"encoding/json"
	"fmt"
	"html/template"
	"io/ioutil"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"

	"github.com/go-redis/redis/v8"
)

var ctx = context.Background()
var rdb *redis.Client

type Project struct {
	Number   string   `json:"number"`
	Versions []string `json:"versions"`
}

func init() {
	rdb = redis.NewClient(&redis.Options{Addr: "localhost:6379", Password: "", DB: 0})
	if _, err := rdb.Ping(ctx).Result(); err != nil {
		log.Fatalf("Could not connect to Redis: %v", err)
	}
}

func fetchProjectsFromRedis() ([]Project, error) {
	val, err := rdb.Get(ctx, "projects:list").Result()
	if err == redis.Nil {
		return getProjectsInfo()
	} else if err != nil {
		return nil, err
	}
	var projects []Project
	if err := json.Unmarshal([]byte(val), &projects); err != nil {
		return nil, err
	}
	return projects, nil
}

// --- CORRECTED homeHandler ---
func homeHandler(w http.ResponseWriter, r *http.Request) {
	projects, err := fetchProjectsFromRedis()
	if err != nil {
		http.Error(w, "Error fetching projects data.", http.StatusInternalServerError)
		log.Printf("Error fetching projects from Redis: %v", err)
		return
	}

	// Determine the current project and version *before* rendering the template.
	var currentProject Project
	var currentVersion string
	var currentURL string
	if len(projects) > 0 {
		currentProject = projects[0] // The first project is the current one
		if len(currentProject.Versions) > 0 {
			// The last version in the sorted list is the latest.
			currentVersion = currentProject.Versions[len(currentProject.Versions)-1]
			// Calculate the current URL
			currentURL = fmt.Sprintf("https://goldpluto.com/project%s/%s/index.html", currentProject.Number, currentVersion)
		}
	}

	// The data struct now includes the current project, version, and URL.
	data := struct {
		Projects       []Project
		CurrentProject Project
		CurrentVersion string
		CurrentURL     string
	}{
		Projects:       projects,
		CurrentProject: currentProject,
		CurrentVersion: currentVersion,
		CurrentURL:     currentURL,
	}

	funcMap := template.FuncMap{
		"join": strings.Join,
		"json": func(v interface{}) (template.JS, error) {
			a, err := json.Marshal(v)
			if err != nil {
				return "", err
			}
			return template.JS(a), nil
		},
	}

	tmpl, err := template.New("render.html").Funcs(funcMap).ParseFiles("render.html")
	if err != nil {
		http.Error(w, "Error parsing HTML template.", http.StatusInternalServerError)
		log.Printf("Error parsing template: %v", err)
		return
	}

	if err := tmpl.Execute(w, data); err != nil {
		http.Error(w, "Error executing template.", http.StatusInternalServerError)
		log.Printf("Error executing template: %v", err)
	}
}

func getProjectsInfo() ([]Project, error) {
	var projectsInfo []Project
	projectsDir := "/mnt/fastserver/projects"
	if _, err := os.Stat(projectsDir); os.IsNotExist(err) {
		return projectsInfo, nil
	}
	files, err := ioutil.ReadDir(projectsDir)
	if err != nil {
		return nil, err
	}
	for _, f := range files {
		if f.IsDir() && strings.HasPrefix(f.Name(), "project") {
			projectNumber := strings.TrimPrefix(f.Name(), "project")
			var versions []string
			versionPath := filepath.Join(projectsDir, f.Name())
			versionFiles, err := ioutil.ReadDir(versionPath)
			if err == nil {
				for _, vf := range versionFiles {
					if vf.IsDir() && strings.HasPrefix(vf.Name(), "v") {
						versions = append(versions, vf.Name())
					}
				}
			}
			sort.Slice(versions, func(i, j int) bool {
				v1, _ := strconv.Atoi(strings.TrimPrefix(versions[i], "v"))
				v2, _ := strconv.Atoi(strings.TrimPrefix(versions[j], "v"))
				return v1 < v2
			})
			projectsInfo = append(projectsInfo, Project{Number: projectNumber, Versions: versions})
		}
	}
	sort.Slice(projectsInfo, func(i, j int) bool {
		n1, _ := strconv.Atoi(projectsInfo[i].Number)
		n2, _ := strconv.Atoi(projectsInfo[j].Number)
		return n1 > n2
	})
	projectsJSON, err := json.Marshal(projectsInfo)
	if err == nil {
		rdb.Set(ctx, "projects:list", projectsJSON, 0)
	}
	return projectsInfo, nil
}

func main() {
	http.HandleFunc("/go", homeHandler)
	// The periodic update from the filesystem is removed.
	// The Python service is now responsible for keeping the 'projects:list' Redis key up to date.
	fmt.Println("Server starting on port 8080...")
	if err := http.ListenAndServe("127.0.0.1:8080", nil); err != nil {
		log.Fatalf("Server failed to start: %v", err)
	}
}