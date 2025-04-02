import logging
import re
from datetime import datetime, UTC
from fastapi import FastAPI, Depends, HTTPException, Request, status, Form, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from typing import List
from bson import ObjectId
import os
from config import MongoDBConfig
from amos.manage_mission import process_single_mission, mine_asteroid
from utils.auth import create_access_token, get_current_user, get_optional_user, record_login_attempt, check_login_attempts, validate_alphanumeric, pwd_context
from models.models import User, UserCreate, UserUpdate, MissionStart, Token, MissionModel, PyInt64
import random
import plotly.graph_objects as go
from plotly.subplots import make_subplots

app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

db = MongoDBConfig.get_database()
users_collection = db["users"]
login_attempts_collection = db["login_attempts"]

# --- Helper Functions ---
def get_random_asteroids(travel_days: int, limit: int = 3) -> List[dict]:
    logging.info(f"Fetching asteroids with moid_days = {travel_days}")
    matching_asteroids = list(db.asteroids.find({"moid_days": travel_days}))
    if not matching_asteroids:
        logging.warning(f"No asteroids found with moid_days = {travel_days}")
        return []
    return random.sample(matching_asteroids, min(limit, len(matching_asteroids)))

# --- Authentication Endpoints ---
@app.get("/", response_class=HTMLResponse)
async def get_index(request: Request, show_register: bool = False, error: str = None, travel_days: int = None, current_user: User = Depends(get_optional_user)):
    missions = list(db.missions.find({"user_id": current_user.id})) if current_user else []
    asteroids = get_random_asteroids(travel_days) if travel_days and current_user else []
    logging.info(f"User {current_user.username if current_user else 'Anonymous'}: Loaded {len(missions)} missions and {len(asteroids)} asteroids")
    return templates.TemplateResponse("index.html", {
        "request": request,
        "show_register": show_register,
        "error": error,
        "user": current_user,
        "missions": missions,
        "asteroids": asteroids,
        "travel_days": travel_days
    })

@app.post("/register", response_class=RedirectResponse)
async def register(response: Response, username: str = Form(...), email: str = Form(...), password: str = Form(...), company_name: str = Form(default="Unnamed Company")):
    validate_alphanumeric(username, "Username")
    validate_alphanumeric(company_name, "Company Name")
    if users_collection.find_one({"username": username}):
        raise HTTPException(status_code=400, detail="Username already registered")
    if users_collection.find_one({"email": email}):
        raise HTTPException(status_code=400, detail="Email already registered")
    user_dict = UserCreate(username=username, email=email, password=password, company_name=company_name).dict()
    user_dict["_id"] = ObjectId()
    user_dict["hashed_password"] = pwd_context.hash(password)
    user_dict["bank"] = PyInt64(0)
    user_dict["loan_count"] = 0
    user_dict["current_loan"] = PyInt64(0)
    users_collection.insert_one(user_dict)
    access_token = create_access_token(data={"sub": str(user_dict["_id"])})
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="access_token", value=access_token, httponly=True)
    return response

@app.post("/login", response_class=RedirectResponse)
async def login(response: Response, request: Request, username: str = Form(...), password: str = Form(...)):
    validate_alphanumeric(username, "Username")
    attempt_count, lockout_until = check_login_attempts(username)
    if lockout_until:
        remaining_time = (lockout_until - datetime.utcnow()).total_seconds() // 60
        error_msg = f"Too many failed attempts. Locked out until {lockout_until.strftime('%H:%M:%S UTC')} (~{int(remaining_time)} minutes)"
        logging.warning(f"User {username} login locked out until {lockout_until}")
        return RedirectResponse(url=f"/?error={error_msg}", status_code=status.HTTP_303_SEE_OTHER)

    user = users_collection.find_one({"username": username})
    if not user or not pwd_context.verify(password, user.get("hashed_password", user.get("password_hash"))):
        record_login_attempt(username, success=False)
        attempt_count += 1
        error_msg = f"Invalid credentials. {5 - attempt_count} attempts remaining" if attempt_count < 5 else f"Too many failed attempts. Locked out for 5 minutes"
        logging.info(f"User {username} login failed: {'no such user' if not user else 'wrong password'}, {5 - attempt_count if attempt_count < 5 else 0} attempts left")
        return RedirectResponse(url=f"/?error={error_msg}", status_code=status.HTTP_303_SEE_OTHER)
    
    record_login_attempt(username, success=True)
    access_token = create_access_token(data={"sub": str(user["_id"])})
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(key="access_token", value=access_token, httponly=True)
    logging.info(f"User {username} logged in successfully")
    return response

@app.get("/logout", response_class=RedirectResponse)
async def logout(response: Response):
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie("access_token")
    return response

@app.patch("/users/me", response_model=User)
async def update_user(update: UserUpdate, user: User = Depends(get_current_user)):
    update_dict = update.dict(exclude_unset=True)
    if "company_name" in update_dict:
        validate_alphanumeric(update_dict["company_name"], "Company Name")
    users_collection.update_one({"_id": ObjectId(user.id)}, {"$set": update_dict})
    if "company_name" in update_dict:
        db.missions.update_many({"user_id": user.id}, {"$set": {"company": update_dict["company_name"]}})
        logging.info(f"User {user.username}: Updated company name to {update_dict['company_name']} for all missions")
    updated_user = users_collection.find_one({"_id": ObjectId(user.id)})
    return User(**{**updated_user, "_id": str(updated_user["_id"])})

# --- Mission Endpoints ---
@app.post("/missions/start", response_class=RedirectResponse)
async def start_mission(
    asteroid_full_name: str = Form(...),
    ship_name: str = Form(...),
    travel_days: int = Form(...),
    user: User = Depends(get_current_user),
    response: Response = None
):
    logging.info(f"User {user.username}: Starting mission with asteroid {asteroid_full_name}, ship {ship_name}, travel_days {travel_days}")
    validate_alphanumeric(ship_name, "Ship Name")
    
    existing_ship = db.ships.find_one({"user_id": user.id, "name": ship_name, "location": 0.0, "active": True})
    ship_id = str(existing_ship["_id"]) if existing_ship else None
    if not ship_id:
        unavailable_ship = db.ships.find_one({"user_id": user.id, "name": ship_name})
        if unavailable_ship:
            logging.warning(f"User {user.username}: Ship {ship_name} exists but is unavailable (location: {unavailable_ship['location']}, active: {unavailable_ship['active']})")
            return RedirectResponse(url="/?error=Ship is currently unavailable (not at Earth or inactive)", status_code=status.HTTP_303_SEE_OTHER)
        
        ship_data = {
            "_id": ObjectId(),
            "name": ship_name,
            "user_id": user.id,
            "shield": 100,
            "mining_power": 500,
            "created": datetime.now(UTC),
            "days_in_service": 0,
            "location": 0.0,
            "mission": 0,
            "hull": 100,
            "cargo": [],
            "capacity": 50000,
            "active": True,
            "missions": []
        }
        db.ships.insert_one(ship_data)
        ship_id = str(ship_data["_id"])
        logging.info(f"User {user.username}: Created new ship {ship_name} with ID {ship_id}")

    asteroid = db.asteroids.find_one({"full_name": asteroid_full_name})
    if not asteroid:
        logging.error(f"User {user.username}: No asteroid found with full_name {asteroid_full_name}")
        return RedirectResponse(url=f"/?error=No asteroid found with name {asteroid_full_name}", status_code=status.HTTP_303_SEE_OTHER)
    
    from amos.mine_asteroid import calculate_confidence
    mining_power = existing_ship["mining_power"] if existing_ship else 500
    target_yield_kg = existing_ship["capacity"] if existing_ship else 50000
    daily_yield_rate = PyInt64(mining_power * 24 * 0.10 * 3)
    confidence, profit_min, profit_max = calculate_confidence(asteroid["moid_days"], mining_power, target_yield_kg, daily_yield_rate)
    mission_projection = PyInt64(profit_max)

    mission_budget = 400000000
    MINIMUM_FUNDING = 436000000
    if user.bank >= MINIMUM_FUNDING:
        pass
    else:
        loan_multipliers = [1.1, 1.2, 1.3, 1.5, 1.75, 2.0, 2.5]
        multiplier_index = min(user.loan_count, len(loan_multipliers) - 1)
        repayment_rate = loan_multipliers[multiplier_index]
        loan_amount = PyInt64(int(mission_budget * repayment_rate))
        db.users.update_one({"_id": ObjectId(user.id)}, {"$set": {"current_loan": loan_amount}, "$inc": {"loan_count": 1}})
        logging.info(f"User {user.username}: Mission funded with loan of ${loan_amount:,} at {repayment_rate}x (Loan #{user.loan_count + 1})")

    mission_data = {
        "_id": ObjectId(),
        "user_id": user.id,
        "company": user.company_name,
        "ship_name": ship_name,
        "ship_id": ship_id,
        "asteroid_full_name": asteroid_full_name,
        "name": f"{asteroid_full_name} Mission",
        "travel_days_allocated": travel_days,
        "mining_days_allocated": 0,
        "total_duration_days": 0,
        "scheduled_days": 0,
        "budget": mission_budget,
        "status": 0,
        "elements": [],
        "elements_mined": {},
        "cost": 0,
        "revenue": 0,
        "profit": 0,
        "penalties": 0,
        "investor_repayment": 0,
        "ship_repair_cost": 0,
        "previous_debt": 0,
        "events": [],
        "daily_summaries": [],
        "rocket_owned": True,
        "yield_multiplier": 1.0,
        "revenue_multiplier": 1.0,
        "travel_yield_mod": 1.0,
        "travel_delays": 0,
        "target_yield_kg": PyInt64(target_yield_kg),
        "ship_location": 0.0,
        "total_yield_kg": PyInt64(0),
        "days_into_mission": 0,
        "days_left": 0,
        "mission_cost": PyInt64(0),
        "mission_projection": mission_projection,
        "confidence": confidence
    }
    result = db.missions.insert_one(mission_data)
    mission_id = str(result.inserted_id)
    
    if existing_ship:
        db.ships.update_one({"_id": ObjectId(ship_id)}, {"$push": {"missions": mission_id}})
    
    logging.info(f"User {user.username}: Created mission {mission_id} for asteroid {asteroid_full_name} with ship {ship_name}, Projected Profit: ${mission_projection:,}, Confidence: {confidence:.2f}%")
    return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)

@app.post("/missions/advance", response_class=JSONResponse)
async def advance_all_missions(user: User = Depends(get_current_user)):
    active_missions = list(db.missions.find({"user_id": user.id, "status": 0}))
    if not active_missions:
        logging.info(f"User {user.username}: No active missions to advance")
        return JSONResponse(content={"message": "No active missions to advance"}, status_code=200)
    next_day = max([len(m.get("daily_summaries", [])) for m in active_missions], default=0) + 1
    logging.info(f"User {user.username}: Advancing day {next_day} for {len(active_missions)} active missions")
    result = mine_asteroid(user.id, day=next_day, username=user.username, company_name=user.company_name)
    return result

@app.post("/missions/complete", response_class=JSONResponse)
async def complete_all_missions(user: User = Depends(get_current_user)):
    active_missions = list(db.missions.find({"user_id": user.id, "status": 0}))
    if not active_missions:
        logging.info(f"User {user.username}: No active missions to complete")
        return JSONResponse(content={"message": "No active missions to complete"}, status_code=200)
    
    logging.info(f"User {user.username}: Running simulation to complete {len(active_missions)} active missions")
    results = {}
    for mission_raw in active_missions:
        mission_id = str(mission_raw["_id"])
        days_into_mission = len(mission_raw.get("daily_summaries", []))
        while True:
            days_into_mission += 1
            result = process_single_mission(mission_raw, day=days_into_mission, username=user.username, company_name=user.company_name)
            mission_raw = db.missions.find_one({"_id": ObjectId(mission_id)})
            if "status" in result and result["status"] == 1 and result["ship_location"] == 0:
                profit = result.get("profit", 0)
                if profit > 0 and user.current_loan > 0:
                    net_profit = max(0, profit - user.current_loan)
                    db.users.update_one({"_id": ObjectId(user.id)}, {"$inc": {"bank": PyInt64(net_profit)}, "$set": {"current_loan": PyInt64(0)}})
                    logging.info(f"User {user.username}: Mission {mission_id} completed, profit ${profit:,}, repaid loan ${user.current_loan:,}, net to bank ${net_profit:,}")
                elif profit > 0:
                    db.users.update_one({"_id": ObjectId(user.id)}, {"$inc": {"bank": PyInt64(profit)}})
                    logging.info(f"User {user.username}: Mission {mission_id} completed, added profit ${profit:,} to bank")
                results[mission_id] = result
                break
            results[mission_id] = result
    
    return JSONResponse(content=results, status_code=200)

# --- Dashboard and Leaderboard Endpoints ---
@app.get("/dashboard", response_class=HTMLResponse)
async def get_dashboard(request: Request, user: User = Depends(get_current_user)):
    missions = [MissionModel(**m) for m in db.missions.find({"user_id": user.id})]
    for mission in missions:
        ship = db.ships.find_one({"name": mission.ship_name, "user_id": user.id})
        mission.ship_id = str(ship["_id"]) if ship else None
    logging.info(f"User {user.username}: Loaded {len(missions)} missions for dashboard")
    return templates.TemplateResponse("dashboard.html", {"request": request, "missions": missions, "user": user})

@app.get("/dashboard/{mission_id}", response_class=HTMLResponse)
async def get_mission_details(request: Request, mission_id: str, user: User = Depends(get_current_user)):
    mission_dict = db.missions.find_one({"_id": ObjectId(mission_id), "user_id": user.id})
    if not mission_dict:
        raise HTTPException(status_code=404, detail="Mission not found")
    mission = MissionModel(**mission_dict)
    ship = db.ships.find_one({"name": mission.ship_name, "user_id": user.id})
    ship_id = str(ship["_id"]) if ship else None
    logging.info(f"User {user.username}: Loaded mission {mission_id} details with ship ID {ship_id}")
    return templates.TemplateResponse("mission_details.html", {"request": request, "mission": mission, "ship_id": ship_id})

@app.get("/ships/{ship_id}", response_class=HTMLResponse)
async def get_ship_details(request: Request, ship_id: str, user: User = Depends(get_current_user)):
    ship = db.ships.find_one({"_id": ObjectId(ship_id), "user_id": user.id})
    if not ship:
        raise HTTPException(status_code=404, detail="Ship not found")
    
    missions = list(db.missions.find({"ship_name": ship["name"], "user_id": user.id}))
    daily_yields = {}
    for mission in missions:
        for summary in mission.get("daily_summaries", []):
            day = summary["day"]
            elements = summary.get("elements_mined", {}) or {}
            daily_yields[day] = daily_yields.get(day, {})
            for elem, kg in elements.items():
                daily_yields[day][elem] = daily_yields[day].get(elem, 0) + kg

    days = sorted(daily_yields.keys())
    elements = set()
    for yields in daily_yields.values():
        elements.update(yields.keys())
    elements = list(elements)
    colors = ['#FF6B6B', '#4ECDC4', '#45B7D1', '#96CEB4', '#FFEEAD']
    
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    for i, element in enumerate(elements):
        fig.add_trace(go.Bar(x=days, y=[daily_yields.get(day, {}).get(element, 0) for day in days], name=element, marker_color=colors[i % len(colors)]))
    total_yield = [sum(daily_yields.get(day, {}).values()) for day in days]
    fig.add_trace(go.Scatter(x=days, y=total_yield, name="Total Yield (kg)", line=dict(color="#00d4ff", width=2), yaxis="y2"), secondary_y=True)
    fig.update_layout(barmode='stack', title_text=f"Yield History for Ship {ship['name']}", xaxis_title="Day", yaxis_title="Mass Mined (kg)", yaxis2_title="Total Yield (kg)", template="plotly_dark", height=400)
    graph_html = fig.to_html(full_html=False, include_plotlyjs='cdn')

    logging.info(f"User {user.username}: Loaded ship {ship_id} details with {len(missions)} missions")
    return templates.TemplateResponse("ship_details.html", {"request": request, "ship": ship, "missions": missions, "graph_html": graph_html})

@app.get("/leaderboard", response_class=HTMLResponse)
async def get_leaderboard(request: Request, user: User = Depends(get_current_user)):
    USE_CASES = ["fuel", "lifesupport", "energystorage", "construction", "electronics", "coolants", "industrial", "medical", "propulsion", "shielding", "agriculture", "mining"]
    element_uses = {elem["name"]: elem.get("uses", []) for elem in db.elements.find()}
    
    pipeline = [{"$match": {}}, {"$lookup": {"from": "missions", "let": {"userId": {"$toString": "$_id"}}, "pipeline": [{"$match": {"$expr": {"$eq": ["$user_id", "$$userId"]}}}], "as": "missions"}}, {"$project": {"user_id": "$_id", "company": "$company_name", "missions": 1}}]
    all_users = list(users_collection.aggregate(pipeline))
    logging.info(f"Loaded {len(all_users)} users for leaderboard")

    leaderboard_data = []
    for entry in all_users:
        total_elements = {}
        total_profit = 0
        use_case_mass = {use: 0 for use in USE_CASES}
        missions = entry.get("missions", [])
        logging.info(f"User {entry['company']}: Processing {len(missions)} missions")
        
        for mission in missions:
            profit = mission.get("profit", 0)
            total_profit += profit if isinstance(profit, (int, float)) else 0
            elements = mission.get("elements", [])
            for elem in elements:
                name = elem.get("name", "")
                mass_kg = elem.get("mass_kg", 0)
                if isinstance(mass_kg, (int, float)):
                    total_elements[name] = total_elements.get(name, 0) + mass_kg
                    uses = element_uses.get(name, [])
                    for use in uses:
                        if use in USE_CASES:
                            use_case_mass[use] += mass_kg
        
        total_mass = sum(total_elements.values())
        leaderboard_data.append({"user_id": str(entry["user_id"]), "company": entry["company"], "total_profit": total_profit, "total_elements": total_elements, "use_case_mass": use_case_mass, "score": total_profit + total_mass * 1000})
        logging.info(f"User {entry['company']}: Total Profit: {total_profit}, Use Case Mass: {use_case_mass}")

    leaderboard_data.sort(key=lambda x: x["total_profit"], reverse=True)
    logging.info(f"Leaderboard data after sorting: {len(leaderboard_data)} entries")

    for i, entry in enumerate(leaderboard_data, 1):
        entry["rank"] = i

    user_entry = next((e for e in leaderboard_data if e["user_id"] == user.id), None)
    user_rank = user_entry["rank"] if user_entry else len(leaderboard_data) + 1
    top_10 = leaderboard_data[:10]
    if user_entry and user_entry not in top_10:
        top_10.append(user_entry)

    if top_10:
        fig = go.Figure()
        for entry in top_10:
            use_cases = list(entry["use_case_mass"].keys())
            masses = list(entry["use_case_mass"].values())
            fig.add_trace(go.Bar(x=use_cases, y=masses, name=entry["company"], text=[f"{m:,} kg" for m in masses], textposition="auto"))
        fig.update_layout(barmode='group', title_text="Total Mass by Use Case", xaxis_title="Use Case", yaxis_title="Total Mass (kg)", template="plotly_dark", height=600)
        graph_html = fig.to_html(full_html=False, include_plotlyjs='cdn')
    else:
        graph_html = "<p>No data available</p>"

    logging.info(f"User {user.company_name}: Loaded leaderboard, Rank: {user_rank}")
    return templates.TemplateResponse("leaderboard.html", {"request": request, "leaderboard": top_10, "user_rank": user_rank, "user_company": user.company_name, "graph_html": graph_html})