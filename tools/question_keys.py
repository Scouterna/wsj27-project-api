"""
Stable short keys for every question in the two WSJ27 registration forms.

The forms are closed and will not change, so this table can be frozen and
shipped to the GUI as a static mapping (see question_key_map.json, generated
by decode_participants.py).

Keys are camelCase, English, and semantic - deliberately not derived from the
Swedish question text, so the GUI reads "sensitivityToUnpredictability" rather
than the full sentence. The same key is reused across both forms wherever the
two forms ask the same thing, so the GUI only has one vocabulary to learn.
Question ids are unique across both forms (their id sets are disjoint).
"""

# Scoutnet contact data mirrored into the form. These ARE passed through to the
# client, in the template like every other question: what to display is the
# GUI's decision, not the API's - ScoutView shows them too. Kept as a named set
# because they are a snapshot from application time, not the live profile.
SCOUTNET_MIRROR_KEYS = {
    "email",
    "alternateEmail",
    "mobilePhone",
    "nextOfKin1Name",
    "nextOfKin1Email",
    "nextOfKin1Phone",
    "nextOfKin1Relation",
    "nextOfKin2Name",
    "nextOfKin2Email",
    "nextOfKin2Phone",
    "nextOfKin2Relation",
}

# Staff-only administrative fields ("Intern information" tab), filled in by the
# organisation rather than by the applicant.
INTERNAL_KEYS = {
    "registrationStatus",
    "healthPatrolAssessment",
    "healthPatrolAssessorId",
    "internalNote",
    "criminalRecordCheckedBy",
    "unitNumber",
    "scoutViewAccessType",
}

# Kept out of the template: staff-only fields the applicant never sees.
NON_ANSWER_KEYS = INTERNAL_KEYS

# Whole tabs left out of forms_data because the data is consumed elsewhere:
# international scouting/travel experience, activity prerequisites, buddy
# requests and the ID card name.
EXCLUDED_TABS = {"WSJ-relaterad information"}

QUESTION_KEYS: dict[str, str] = {
    # --- Application / registration type -------------------------------
    "84942": "applicationType",  # Typ av ansökan (Deltagare / IST)
    "90951": "applicationType",  # Typ av anmälan (Avdelningsledare / Kontingentledning)
    "84941": "participantTravelType",  # Deltagandetyp (rundresa / direktresa)
    "85095": "istTravelType",  # Funktionärstyp (rundresa / egen resa)
    "93357": "leaderTravelType",  # Med rundresa eller direktresa
    # --- Contact details already in Scoutnet ---------------------------
    "85097": "email",
    "85099": "alternateEmail",
    "85100": "mobilePhone",
    # --- Next of kin ---------------------------------------------------
    # The leaders' form (47115) asks the same questions under its own ids,
    # published only from 2026-09-01. Two of its labels are worded slightly
    # differently ("Mobilnummer", and "Närstående - Relation" missing its "2");
    # the template normalises those to the participant wording so the shared key
    # keeps one label. Ids 90961-90964 run in sequence, which is what identifies
    # the unnumbered relation field as next of kin 2's.
    "90954": "email",
    "90955": "alternateEmail",
    "90956": "mobilePhone",
    "90957": "nextOfKin1Name",
    "90958": "nextOfKin1Email",
    "90959": "nextOfKin1Phone",
    "90960": "nextOfKin1Relation",
    "90961": "nextOfKin2Name",
    "90962": "nextOfKin2Email",
    "90963": "nextOfKin2Phone",
    "90964": "nextOfKin2Relation",
    # --- Emergency contacts, named instead of the next of kin (leaders only) --
    "90975": "hasAlternateEmergencyContact",
    "90976": "emergencyContact1Name",
    "90977": "emergencyContact1Email",
    "90978": "emergencyContact1Phone",
    "90979": "emergencyContact1Relation",
    "90980": "emergencyContact2Name",
    "90981": "emergencyContact2Email",
    "90982": "emergencyContact2Phone",
    "90983": "emergencyContact2Relation",
    "85101": "nextOfKin1Name",
    "85102": "nextOfKin1Email",
    "85103": "nextOfKin1Phone",
    "87647": "nextOfKin1Relation",
    "85104": "nextOfKin2Name",
    "85105": "nextOfKin2Email",
    "85106": "nextOfKin2Phone",
    "88137": "nextOfKin2Relation",
    # --- Language skills (leaders only), self-rated 0-5 -----------------
    "90971": "languageEnglish",
    "90972": "languageFrench",
    "90973": "languageSpanish",
    "90974": "languageArabic",
    # --- Diet ----------------------------------------------------------
    "93221": "specialDiet",  # enum: Vegetarian / Vegan / Halal / ... / Ingen specialkost
    "86889": "specialDiet",
    "93222": "specialDietDetails",
    "88716": "specialDietDetails",
    # --- Food allergies / intolerances ---------------------------------
    "93223": "hasFoodAllergy",
    "88163": "hasFoodAllergy",
    "93225": "foodAllergyLegumes",  # Baljväxter, severity 1-5
    "88153": "foodAllergyLegumes",
    "93226": "foodAllergyFish",
    "88151": "foodAllergyFish",
    "93227": "foodAllergyFruit",
    "88159": "foodAllergyFruit",
    "93228": "foodAllergyGluten",
    "88155": "foodAllergyGluten",
    "93229": "foodAllergyVegetables",
    "88160": "foodAllergyVegetables",
    "93230": "foodAllergyLactose",
    "88147": "foodAllergyLactose",
    "93231": "foodAllergyMilkProtein",
    "88148": "foodAllergyMilkProtein",
    "93232": "foodAllergyNuts",
    "88150": "foodAllergyNuts",
    "93233": "foodAllergyShellfish",
    "88152": "foodAllergyShellfish",
    "93234": "foodAllergyMustard",
    "88154": "foodAllergyMustard",
    "93235": "foodAllergySesame",
    "88157": "foodAllergySesame",
    "93236": "foodAllergyCereals",  # Spannmål
    "88156": "foodAllergyCereals",
    "93237": "foodAllergySulphites",  # Svaveldioxid och sulfit
    "88158": "foodAllergySulphites",
    "93238": "foodAllergyEgg",
    "88146": "foodAllergyEgg",
    "93239": "foodAllergyOther",  # Övriga (Ja/Nej)
    "88161": "foodAllergyOther",
    "93240": "foodAllergyDetails",
    "88162": "foodAllergyDetails",
    # --- Other (non-food) allergies ------------------------------------
    "93241": "hasOtherAllergy",
    "87053": "hasOtherAllergy",
    "93242": "otherAllergyDetails",
    "88178": "otherAllergyDetails",
    # --- Vaccinations --------------------------------------------------
    "93244": "childhoodVaccinationsComplete",
    "88717": "childhoodVaccinationsComplete",
    "93247": "tetanusBoosterAsAdult",
    "91768": "tetanusBoosterAsAdult",
    "93248": "tetanusBoosterYear",
    "91766": "tetanusBoosterYear",
    "93245": "diphtheriaBoosterAsAdult",
    "88718": "diphtheriaBoosterAsAdult",
    "93246": "diphtheriaBoosterYear",
    "91767": "diphtheriaBoosterYear",
    # --- Medication ----------------------------------------------------
    "93249": "usesPrescriptionMedication",
    "89840": "usesPrescriptionMedication",
    "93250": "medicationDetails",  # which drugs and dosage
    "92454": "medicationDetails",
    "93254": "medicationStorageNeeded",  # Behöver denna förvaras på något speciellt sätt?
    "93252": "medicationStorageNeeded",
    "93251": "medicationStorageDetails",  # "Hur då?" - verified follow-up to the above
    "89841": "medicationStorageDetails",
    "93253": "managesOwnMedication",  # participants/IST only
    # --- Illness / medical conditions ----------------------------------
    "93255": "hasMedicalCondition",
    "91238": "hasMedicalCondition",
    "93256": "medicalConditionDetails",
    "91239": "medicalConditionDetails",
    # --- Medical equipment ---------------------------------------------
    "93257": "needsMedicalEquipment",
    "87058": "needsMedicalEquipment",
    "93258": "medicalEquipmentNeeds",  # multi-choice: power, cooling, CPAP charging...
    "87059": "medicalEquipmentNeeds",
    "93259": "medicalEquipmentDetails",  # free text "Berätta vad du behöver"
    "87313": "medicalEquipmentDetails",
    # --- Physical limitations ------------------------------------------
    "93260": "hasPhysicalLimitations",
    "87060": "hasPhysicalLimitations",
    "93261": "physicalLimitationsDetails",
    "87061": "physicalLimitationsDetails",
    # --- Mobility aids --------------------------------------------------
    "93262": "mobilityAids",  # multi-choice: kryckor, käpp, större tält, andra hjälpmedel
    "87062": "mobilityAids",
    "93264": "otherMobilityAidsDetails",
    "87063": "otherMobilityAidsDetails",
    # --- Cognitive diagnoses / mental health ----------------------------
    "93265": "cognitiveDiagnoses",  # multi-choice list of diagnoses
    "91796": "cognitiveDiagnoses",
    "93266": "cognitiveDiagnosesDetails",  # impact + support needed
    "87066": "cognitiveDiagnosesDetails",
    "93267": "hasPhobia",
    "91266": "hasPhobia",
    "93268": "phobiaDetails",
    "91267": "phobiaDetails",
    "93269": "hasMentalHealthCondition",  # anxiety / eating disorder / PTSD
    "91268": "hasMentalHealthCondition",
    "93270": "mentalHealthDetails",
    "91269": "mentalHealthDetails",
    # --- Personal prerequisites (participants/IST only) -----------------
    "87311": "needsPersonalAssistant",
    "88720": "sensitivityToUnpredictability",  # irregular meals, late changes, little own time
    "89842": "needsSupportForUnpredictability",
    "89888": "unpredictabilityDetails",
    # --- WSJ activity prerequisites -------------------------------------
    "88354": "canSwim200m",
    "88356": "comfortableInLargeCrowds",
    "88360": "activityPrerequisitesDetails",
    # --- International scouting experience ------------------------------
    "87653": "hasInternationalScoutingExperience",
    "87654": "internationalScoutingExperienceDetails",
    "87655": "hasIndependentTravelExperience",
    "87656": "independentTravelExperienceDetails",
    # --- Buddy requests (who they want to be placed with) ---------------
    "87662": "buddyRequest1Name",
    "87660": "buddyRequest1MemberNo",
    "87665": "buddyRequest2Name",
    "87663": "buddyRequest2MemberNo",
    # --- Misc ------------------------------------------------------------
    "88364": "idCardName",
    "93211": "additionalInfo",  # "anything else we should know" - 3 wordings, one meaning
    "87312": "additionalInfoUnitLeader",
    "93212": "additionalInfo",
    # --- Internal / staff-only fields -----------------------------------
    "88168": "unitNumber",  # Avdelning
    "107592": "unitNumber",
    "107593": "registrationStatus",
    "88169": "healthPatrolAssessment",
    "88170": "healthPatrolAssessorId",
    "88171": "internalNote",
    "88172": "criminalRecordCheckedBy",
    "110268": "scoutViewAccessType",
}
